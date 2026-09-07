"""Background scrape jobs for the lead engine.

The rep asks for a new search from the leads screen. That search opens a real
Chromium window, drives Google Maps, audits every site it finds and scores the
lot, which takes minutes. An HTTP request cannot sit and wait for that, so the
work is handed to a child process and the request comes back at once with a job
id. The UI then polls the job and shows the last line the scraper printed, which
is the whole point of keeping the output: a browser window opening and scrolling
for four minutes with nothing on screen looks broken, and the rep kills it.

What this module owns:

* One child process at a time. A second request gets the job that is already
  running instead of a second Chromium. Two Chromium processes would fight over
  the same browser profile directory, and the second one usually dies with a
  lock error, taking the first one down with it.
* The last 200 output lines per job, plus the last line on its own.
* Reading that output in its own task, beside the wait on the child and never
  before it. A scraper that dies badly can leave Chromium alive holding the
  same pipe, so the end of the output never arrives. Reading to the end first
  would then hang on a job that is already over, and one job stuck on
  ``running`` blocks every later search.
* The job record for an hour after the process exits, so a refreshed page can
  still read the result. Older ones are swept.
* Killing the whole process tree on cancel, not just the python child, and
  killing whatever the child left behind when the child itself has gone.

Security note, please do not "simplify" this away: the niche and the city come
from a request body and go onto a command line. They are passed as separate
argv entries to ``asyncio.create_subprocess_exec``, which never goes near a
shell, so a value like ``barber; rm -rf ~`` is just a strange search term and
nothing else. That is why there is no ``shell=True`` here and no string built
with the user values glued into it. On top of that the two values are cleaned
and length checked before anything is launched.

Typical use::

    from app.services import lead_jobs

    job = await lead_jobs.start_scrape("dentist", "lahore", 60, "pk")
    state = lead_jobs.get_job(job["jobId"])
    if state is not None and not state["done"]:
        await lead_jobs.cancel_job(job["jobId"])

``start_scrape`` and ``cancel_job`` are async because they touch the child
process. ``get_job`` and ``list_jobs`` only read memory, so they are plain
functions. Do not await them.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import logging
import os
import re
import signal
import sys
import time
import uuid
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

log = logging.getLogger("salescopilot.lead_jobs")


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]
"""The repository root, which is the cwd the child process needs.

This file is ``<root>/backend/app/services/lead_jobs.py``, so three parents up
is the root. It is worked out from ``__file__`` rather than from the current
working directory because uvicorn is started from ``backend/``, and
``python -m leadengine.run`` only resolves when the process runs from the root.
"""

LEADENGINE_DIR: Final[Path] = REPO_ROOT / "leadengine"
"""Where the lead engine package lives. Only used to give a clear error."""


def _venv_python() -> Path:
    """Find the python that has the scraper's own dependencies installed.

    The API may well be started by some other python, and the scraper needs
    playwright, which lives in the repository venv. So the child is launched
    with that interpreter by path instead of with ``sys.executable``.

    Returns:
        The path of the venv interpreter. When neither the posix nor the
        windows layout is present, the running interpreter is returned so the
        failure shows up as a plain import error in the job output rather than
        as a crash inside this module.
    """
    posix = REPO_ROOT / "backend" / ".venv" / "bin" / "python"
    if posix.exists():
        return posix
    windows = REPO_ROOT / "backend" / ".venv" / "Scripts" / "python.exe"
    if windows.exists():
        return windows
    log.warning("no venv python at %s, falling back to %s", posix, sys.executable)
    return Path(sys.executable)


VENV_PYTHON: Final[Path] = _venv_python()
"""The interpreter used for the child process."""


# --------------------------------------------------------------------------- #
# Tunables
# --------------------------------------------------------------------------- #

MAX_LINES: Final[int] = 200
"""How many output lines are kept per job. The deque drops the oldest for us."""

MAX_LINE_CHARS: Final[int] = 500
"""Longest single line kept. A runaway line cannot eat the process memory."""

MAX_TERM_CHARS: Final[int] = 80
"""Longest niche or city accepted. The engine's own slug caps at 80 anyway."""

MAX_PARTIAL_CHARS: Final[int] = 4000
"""If the child writes this much with no line break, flush it as a line."""

READ_CHUNK: Final[int] = 4096
"""Bytes pulled from the child pipe per read."""

JOB_TTL_S: Final[float] = 3600.0
"""How long a finished job is kept, in seconds. One hour, per the contract."""

MAX_JOBS: Final[int] = 50
"""Hard cap on remembered jobs, so a burst of quick failures cannot pile up."""

TERM_GRACE_S: Final[float] = 5.0
"""How long a cancelled child gets to close itself after SIGTERM."""

KILL_GRACE_S: Final[float] = 5.0
"""How long we wait after SIGKILL before giving up on the wait."""

EXIT_POLL_S: Final[float] = 0.2
"""How often the child is checked for having exited.

``Process.wait`` cannot be trusted on its own here, see :func:`_exit_code`, so
the exit code is polled instead. A search runs for minutes, so a fifth of a
second late is not something anybody can see.
"""

PUMP_GRACE_S: Final[float] = 5.0
"""How long the reader gets to finish after the child has already exited.

Normally it needs none of it. The child's end of the pipe closes when the child
goes, so the reader sees the end of the output at once. It only runs out when
something the child started is still alive and still holding the same pipe.
"""

LEFTOVER_GRACE_S: Final[float] = 3.0
"""How long each signal gets to clear a leftover process holding the pipe."""

FINALISE_GRACE_S: Final[float] = 5.0
"""How long cancel waits for the watcher task to write the final state."""

DEFAULT_LIMIT: Final[int] = 60
"""Leads asked for when the caller does not say."""

MIN_LIMIT: Final[int] = 1
MAX_LIMIT: Final[int] = 400
"""Bounds for the lead count. 400 matches the engine's own safety cap."""

DEFAULT_COUNTRY: Final[str] = "us"
"""Country code used when the caller does not say."""


# --------------------------------------------------------------------------- #
# Step labels, shown to the rep while the job runs
# --------------------------------------------------------------------------- #

STEP_STARTING: Final[str] = "Opening the browser"
STEP_DONE: Final[str] = "Done"
STEP_FAILED: Final[str] = "Stopped with an error"
STEP_STOPPING: Final[str] = "Stopping"
STEP_STOPPED: Final[str] = "You stopped it"

STEP_LABELS: Final[dict[int, str]] = {
    1: "Getting leads from Google Maps",
    2: "Checking each business",
    3: "Scoring the leads",
    4: "Writing the messages",
}
"""Plain English names for the four stages ``leadengine.run`` prints.

The engine prints its own banner in Roman Urdu, for example
``STEP 2/4  ·  Har lead ka deep audit``. The number is read out of that line and
turned into one of these, because the leads screen is read by people who are not
reading Roman Urdu.
"""

_STEP_RE: Final[re.Pattern[str]] = re.compile(r"\bSTEP\s+(\d+)\s*/\s*(\d+)")
"""Matches the step banner the engine prints at the top of every stage."""

_ANSI_RE: Final[re.Pattern[str]] = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
"""Colour and erase codes. The engine writes them even when piped, so strip."""

_SPLIT_RE: Final[re.Pattern[str]] = re.compile(r"\r\n|\r|\n")
"""Line break matcher.

A bare carriage return counts. The scraper draws its live counter with
``\\r\\033[K  [3/60] SEO  Some Salon``, so every progress tick ends with a
carriage return and no newline. Splitting on newlines alone would show the rep
nothing for minutes and then dump the whole run at once.
"""

_CONTROL_RE: Final[re.Pattern[str]] = re.compile(r"[\x00-\x1f\x7f]")
"""Control characters, never allowed in a search term."""

_SLUG_RE: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9]+")
"""Everything that is not a lowercase letter or a digit becomes a dash."""


class InvalidSearchError(ValueError):
    """A niche, city, country or limit that must not reach the command line.

    It subclasses :class:`ValueError`, so a route that catches ``ValueError``
    and answers 400 already handles it. The message is plain English and is
    written to be shown to the rep as it is.
    """


def search_id(niche: str, location: str) -> str:
    """Return the slug the lead engine will name its files with.

    This is a copy of ``leadengine.scrape.search_id`` and has to stay the same
    as it. It is copied rather than imported on purpose: importing that module
    pulls in ``leadengine.scraper.maps``, which imports playwright at module
    level, and the API process must keep booting whether or not playwright is
    installed. Six lines of duplication buys an API that cannot be taken down by
    the scraper's dependencies.

    Args:
        niche: The already cleaned niche, for example ``barber``.
        location: The already cleaned city, for example ``hoboken``.

    Returns:
        The slug, for example ``barber-hoboken``. Empty when neither value has
        a letter or a digit in it.
    """
    slug = _SLUG_RE.sub("-", (niche + "--" + location).lower()).strip("-")
    return slug[:80]


# --------------------------------------------------------------------------- #
# Input cleaning
# --------------------------------------------------------------------------- #


def _clean_term(value: object, label: str, example: str) -> str:
    """Clean and check one free text search term.

    Args:
        value: Whatever arrived in the request body.
        label: The word used in the error message, ``niche`` or ``city``.
        example: A short example used in the error message.

    Returns:
        The cleaned term, with runs of whitespace squeezed to one space.

    Raises:
        InvalidSearchError: When the value is empty, too long, starts with a
            dash, or carries no letter or digit at all.
    """
    text = "" if value is None else str(value)
    text = _CONTROL_RE.sub(" ", text)
    text = " ".join(text.split())
    if not text:
        raise InvalidSearchError("Type a " + label + ", " + example + ".")
    if len(text) > MAX_TERM_CHARS:
        raise InvalidSearchError(
            "The " + label + " is too long. Keep it under " + str(MAX_TERM_CHARS) + " letters."
        )
    if text.startswith("-"):
        # argparse in the child would read a leading dash as a flag name, not as
        # the positional value, and the job would die on a confusing error.
        raise InvalidSearchError("The " + label + " cannot start with a dash.")
    if not any(char.isalnum() for char in text):
        raise InvalidSearchError("The " + label + " needs some letters or numbers.")
    return text


def _clean_country(value: object) -> str:
    """Clean and check the two letter country code.

    Args:
        value: Whatever arrived in the request body. Empty means the default.

    Returns:
        The lowercase two letter code, for example ``pk``.

    Raises:
        InvalidSearchError: When the value is not two ascii letters.
    """
    text = ("" if value is None else str(value)).strip().lower()
    if not text:
        return DEFAULT_COUNTRY
    if len(text) != 2 or not text.isascii() or not text.isalpha():
        raise InvalidSearchError("Use a two letter country code, like us or pk.")
    return text


def _clean_limit(value: object) -> int:
    """Clean the lead count and pull it inside the safe range.

    A number outside the range is clamped rather than refused. The field is a
    small box on a form, and stopping the whole search because someone typed an
    extra zero helps nobody.

    Args:
        value: Whatever arrived in the request body.

    Returns:
        An int between :data:`MIN_LIMIT` and :data:`MAX_LIMIT`.

    Raises:
        InvalidSearchError: When the value is not a whole number at all.
    """
    if value is None or value == "":
        return DEFAULT_LIMIT
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise InvalidSearchError("How many leads has to be a number.") from None
    return max(MIN_LIMIT, min(MAX_LIMIT, number))


# --------------------------------------------------------------------------- #
# The job record
# --------------------------------------------------------------------------- #


@dataclass
class _Job:
    """One scrape run, live or finished.

    Attributes:
        id: The job id handed to the UI.
        niche: The cleaned niche the child was launched with.
        location: The cleaned city the child was launched with.
        limit: How many leads were asked for.
        country: The two letter country code.
        search_id: The slug the engine writes its files under, known before the
            child has printed anything, so the UI can select the search the
            moment the job finishes.
        state: ``running``, ``finished`` or ``failed``. Those three are the only
            values the wire ever sees, per the contract. A cancelled job is
            ``failed`` with :attr:`cancelled` set, because the rep stopping a
            search is not a success.
        step: A short plain English line about the stage in progress.
        line: The last line the child printed.
        lines: The last :data:`MAX_LINES` lines the child printed.
        started_at: Unix time the child was launched.
        finished_at: Unix time the child exited, or None while it runs.
        exit_code: The child's exit code, or None if it never gave one.
        error: A plain English reason, only set when something went wrong.
        cancelled: True when the rep stopped it rather than it ending on its own.
        process: The child. Never sent on the wire.
        pgid: The child's process group, noted while the child was still alive.
            Once the child has exited it has been reaped and cannot be asked for
            its group any more, and by then the group is exactly what has to be
            cleaned up. Never sent on the wire.
        task: The watcher task that reads the output and writes the end state.
    """

    id: str
    niche: str
    location: str
    limit: int
    country: str
    search_id: str
    state: str = "running"
    step: str = STEP_STARTING
    line: str = ""
    lines: deque[str] = field(default_factory=lambda: deque(maxlen=MAX_LINES))
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    exit_code: int | None = None
    error: str = ""
    cancelled: bool = False
    process: asyncio.subprocess.Process | None = field(default=None, repr=False)
    pgid: int | None = field(default=None, repr=False)
    task: asyncio.Task[None] | None = field(default=None, repr=False)

    def record_line(self, raw: str) -> None:
        """Keep one line of child output, if there is anything in it.

        Colour codes are stripped, the line is trimmed and blank lines are
        dropped. The engine prints blank lines for spacing, and showing the rep
        an empty status line looks like the job died.

        Args:
            raw: One line as it came off the pipe, with no line break on it.
        """
        text = _ANSI_RE.sub("", raw).strip()
        if not text:
            return
        if len(text) > MAX_LINE_CHARS:
            text = text[:MAX_LINE_CHARS]
        self.lines.append(text)
        self.line = text
        match = _STEP_RE.search(text)
        if match:
            number = int(match.group(1))
            total = int(match.group(2))
            label = STEP_LABELS.get(number, "Working")
            self.step = "Step " + str(number) + " of " + str(total) + ", " + label

    def to_wire(self, *, already_running: bool = False) -> dict[str, Any]:
        """Return the job as a JSON safe dict for the API.

        Args:
            already_running: True when this job is being handed back to a second
                start request instead of a new job. The UI uses it to say that a
                search is already going, rather than pretending it started one.

        Returns:
            A plain dict. ``state``, ``step``, ``line``, ``searchId`` and
            ``done`` are the five keys the contract names. The rest are extras
            the UI can use, and every key is always present so the TypeScript
            side never has to test for a missing field.
        """
        return {
            "jobId": self.id,
            "searchId": self.search_id,
            "niche": self.niche,
            "location": self.location,
            "limit": self.limit,
            "country": self.country,
            "state": self.state,
            "step": self.step,
            "line": self.line,
            "lines": list(self.lines),
            "done": self.state != "running",
            "ok": self.state == "finished",
            "cancelled": self.cancelled,
            "exitCode": self.exit_code,
            "error": self.error,
            "startedAt": self.started_at,
            "finishedAt": self.finished_at,
            "alreadyRunning": already_running,
        }


_jobs: dict[str, _Job] = {}
"""Every job we still remember, newest and oldest together, keyed by job id."""

_start_lock: asyncio.Lock = asyncio.Lock()
"""Guards the check for a running job and the launch that follows it.

A lock really is needed here, unlike in ``session_store``, because starting a
child is an await point. Without it two requests landing in the same tick would
both see no running job, both await ``create_subprocess_exec``, and two Chromium
processes would open on the same profile directory.
"""


# --------------------------------------------------------------------------- #
# Housekeeping
# --------------------------------------------------------------------------- #


def _sweep(now: float | None = None) -> int:
    """Drop finished jobs that are older than the TTL.

    A running job is never swept, whatever its age. Some searches are slow on
    purpose.

    Args:
        now: Unix time to compare against. Defaults to the current time.

    Returns:
        How many jobs were dropped.
    """
    stamp = time.time() if now is None else now
    dropped = 0
    for job_id in list(_jobs):
        job = _jobs[job_id]
        if job.state == "running" or job.finished_at is None:
            continue
        if stamp - job.finished_at > JOB_TTL_S:
            del _jobs[job_id]
            dropped += 1

    # Second pass, a hard cap. Nothing above stops a hundred jobs that each
    # failed in a second from sitting in memory for the full hour.
    if len(_jobs) > MAX_JOBS:
        done = [job for job in _jobs.values() if job.state != "running"]
        done.sort(key=lambda item: item.finished_at or item.started_at)
        for job in done[: len(_jobs) - MAX_JOBS]:
            _jobs.pop(job.id, None)
            dropped += 1
    return dropped


def _running_job() -> _Job | None:
    """Return the one job whose child is still alive, if there is one."""
    for job in _jobs.values():
        if job.state == "running":
            return job
    return None


def _build_command(niche: str, location: str, limit: int, country: str) -> list[str]:
    """Build the child command as a list of separate argv entries.

    There is no shell anywhere in this path. The values are already cleaned, and
    they are still passed as their own list entries, so quoting, semicolons and
    backticks in a search term can never mean anything to anybody.

    The ``-u`` matters. Python block buffers stdout when it is a pipe, so
    without it the banner lines would sit in a 4KB buffer and the rep would
    stare at a spinner while the scraper is halfway done.

    Args:
        niche: The cleaned niche.
        location: The cleaned city.
        limit: How many leads to ask for.
        country: The two letter country code.

    Returns:
        The argv list for ``asyncio.create_subprocess_exec``.
    """
    return [
        str(VENV_PYTHON),
        "-u",
        "-m",
        "leadengine.run",
        niche,
        location,
        "--limit",
        str(limit),
        "--country",
        country,
    ]


def _child_env() -> dict[str, str]:
    """Return the environment for the child.

    The parent environment is passed through, because the scraper reads its own
    ``GMO_`` knobs from it. Two values are forced on so the output arrives line
    by line and never dies on an encoding error.
    """
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


# --------------------------------------------------------------------------- #
# Reading the child output
# --------------------------------------------------------------------------- #


async def _pump(job: _Job, stream: asyncio.StreamReader) -> None:
    """Read the child output and record it line by line until the pipe closes.

    Fixed size reads are used rather than ``readline``. Two reasons, both real.
    The scraper's progress counter only ever writes a carriage return, so
    ``readline`` would block for minutes and then return one enormous line, and
    ``StreamReader.readline`` raises once a line passes its 64KB limit, which
    that counter would reach on a long search.

    An incremental decoder is used so a UTF-8 character split across two reads
    survives, which matters because the engine prints box drawing characters.

    Args:
        job: The job to record the lines on.
        stream: The child's merged stdout and stderr.
    """
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    buffer = ""
    while True:
        chunk = await stream.read(READ_CHUNK)
        if not chunk:
            break
        buffer += decoder.decode(chunk)
        parts = _SPLIT_RE.split(buffer)
        buffer = parts.pop()
        for part in parts:
            job.record_line(part)
        if len(buffer) > MAX_PARTIAL_CHARS:
            job.record_line(buffer)
            buffer = ""
    buffer += decoder.decode(b"", True)
    if buffer:
        job.record_line(buffer)


async def _exit_code(process: asyncio.subprocess.Process) -> int:
    """Wait for the child to exit, whatever happens to its output pipe.

    ``await process.wait()`` looks like the obvious way to do this and it is a
    trap. asyncio only wakes a waiter once the child has exited **and** every
    pipe it was given has reached its end. The proof is in the standard
    library: ``BaseSubprocessTransport._wait`` parks the waiter, and only
    ``_call_connection_lost`` ever wakes it, which ``_try_finish`` only reaches
    when ``all(p.disconnected for p in self._pipes.values())``.

    That is fatal here. Chromium is started by playwright as a grandchild and
    it holds a copy of the same output pipe. A child that dies without taking
    Chromium with it leaves that wait parked for the life of the server, and
    the job with it. ``returncode`` carries no such condition, it is set as
    soon as the child is reaped, so it is polled instead.

    Args:
        process: The child process.

    Returns:
        The child's exit code.
    """
    while True:
        code = process.returncode
        if code is not None:
            return code
        await asyncio.sleep(EXIT_POLL_S)


async def _exited_within(process: asyncio.subprocess.Process, timeout: float) -> bool:
    """Wait a set time for the child to exit.

    Args:
        process: The child process.
        timeout: How many seconds to wait.

    Returns:
        True when the child exited inside the time, False when it did not.
    """
    try:
        await asyncio.wait_for(_exit_code(process), timeout)
    except asyncio.TimeoutError:
        return False
    return True


async def _pump_ended(pump: asyncio.Task[None], timeout: float) -> bool:
    """Wait a little for the reader task to end.

    The wait is shielded, so running out of time here does not cancel the
    reader. There is another thing to try after each timeout, and a reader
    cancelled too early would throw away output that is about to arrive.

    Args:
        pump: The reader task.
        timeout: How many seconds to wait.

    Returns:
        True when the reader finished inside the time, False when it did not.
    """
    try:
        await asyncio.wait_for(asyncio.shield(pump), timeout)
    except asyncio.TimeoutError:
        return False
    return True


async def _stop_pump(pump: asyncio.Task[None] | None) -> None:
    """Cancel the reader task and wait for it to really be gone.

    Args:
        pump: The reader task, or None when there was never one.
    """
    if pump is None or pump.done():
        return
    pump.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await pump


async def _drain(job: _Job, pump: asyncio.Task[None]) -> None:
    """Let the reader catch up now that the child has exited, then move on.

    This is the guard that keeps a job from living for ever. The child writes
    to a pipe, and playwright starts Chromium as a grandchild that inherits the
    very same pipe. When the child dies without taking Chromium with it, a
    crash, a kill, or a browser that got detached, the end of the output never
    arrives and the reader would wait for it until the server is restarted.

    So the reader gets a short while, and if it is still waiting after that the
    leftovers are stopped, first politely and then not. That closes the pipe,
    which ends the reader, and it also frees the browser profile the next
    search needs. If even that does not work, the reader is dropped. Losing the
    tail of the output of a job that is already over costs the rep nothing.
    Leaving the job on ``running`` costs the rep every search after it.

    Args:
        job: The job whose child has already exited.
        pump: The reader task for that child.
    """
    if await _pump_ended(pump, PUMP_GRACE_S):
        return
    log.warning(
        "lead job %s: the child is gone but its output pipe is still open, "
        "something it started is still alive",
        job.id,
    )
    if _signal_leftovers(job, signal.SIGTERM) and await _pump_ended(pump, LEFTOVER_GRACE_S):
        return
    kill = getattr(signal, "SIGKILL", signal.SIGTERM)
    if _signal_leftovers(job, kill) and await _pump_ended(pump, LEFTOVER_GRACE_S):
        return
    log.warning("lead job %s: giving up on reading the rest of the output", job.id)
    await _stop_pump(pump)


def _finish(job: _Job, code: int | None, error: str = "") -> None:
    """Write the end state on a job whose child has stopped.

    The output lines are left exactly as the child printed them. Nothing
    friendly is appended, because ``line`` is documented as the last line the
    job printed and the UI has ``state`` and ``error`` for the rest.

    Args:
        job: The job to close.
        code: The child's exit code, or None when there was never one.
        error: A plain English reason to show, when the caller has a better one
            than the exit code.
    """
    job.exit_code = code
    job.finished_at = time.time()
    if job.cancelled:
        job.state = "failed"
        job.step = STEP_STOPPED
        job.error = error or "You stopped this search."
    elif code == 0:
        job.state = "finished"
        job.step = STEP_DONE
        job.error = error
    else:
        job.state = "failed"
        job.step = STEP_FAILED
        if error:
            job.error = error
        elif code is None:
            job.error = "The search stopped and did not say why."
        else:
            job.error = "The search stopped with error code " + str(code) + "."
    log.info(
        "lead job %s %s in %.1fs (exit %s)",
        job.id,
        job.state,
        job.finished_at - job.started_at,
        code,
    )


async def _watch(job: _Job) -> None:
    """Watch the child to its end, then record how it went.

    The reader runs as its own task beside the wait on the child. It is never
    awaited first, and that ordering is the whole point of this function. The
    child's output is a pipe, and the Chromium that playwright starts holds a
    copy of the same pipe. If the child dies without taking Chromium with it,
    the end of the output never arrives, so reading to the end first would wait
    for ever on a child that has already exited. The job would sit on
    ``running`` for the life of the server, and because only one job may run at
    a time, every later search would be refused with no way back but a
    restart. Watching the child first means a job that is over is always
    recorded as over. The reader is then given a moment to catch up, see
    :func:`_drain`.

    The exit is watched with :func:`_exit_code` and not with
    ``process.wait()``, because that wait waits on the pipes as well and would
    hang in exactly the same case. Read the note on that function before
    changing this back.

    This task owns the end state. Everything else, cancel included, only asks
    the child to stop and then waits for this to write the result, so there is
    one place where a job stops being ``running``.

    Args:
        job: The job whose child is running.
    """
    process = job.process
    if process is None:  # pragma: no cover - only reachable on a coding error
        _finish(job, None, "The search never started.")
        return
    kill = getattr(signal, "SIGKILL", signal.SIGTERM)
    pump: asyncio.Task[None] | None = None
    try:
        if process.stdout is not None:
            pump = asyncio.create_task(_pump(job, process.stdout), name="lead-read-" + job.id)
        code = await _exit_code(process)
        if pump is not None:
            await _drain(job, pump)
    except asyncio.CancelledError:
        # Someone cancelled this task rather than calling cancel_job. Do not let
        # a Chromium window outlive it.
        await _stop_pump(pump)
        await _kill_tree(job)
        _signal_leftovers(job, kill)
        job.cancelled = True
        _finish(job, process.returncode)
        raise
    except Exception as exc:  # pragma: no cover - defensive, pipes rarely blow up
        log.exception("lead job %s could not be read", job.id)
        await _stop_pump(pump)
        await _kill_tree(job)
        _signal_leftovers(job, kill)
        _finish(job, process.returncode, "The search could not be read. " + str(exc))
        return
    _finish(job, code)


# --------------------------------------------------------------------------- #
# Killing the child, and everything it started
# --------------------------------------------------------------------------- #


def _remember_group(job: _Job) -> None:
    """Note the child's process group while the child is still alive.

    It has to be read now. Once the child exits it is reaped, and a reaped pid
    can no longer be asked which group it led. The child is started in its own
    session, so it leads its own group and the group id is its pid, but it is
    read back rather than assumed.

    Args:
        job: The job whose child has just been launched.
    """
    process = job.process
    if process is None or not process.pid or process.pid <= 0:
        return
    if not hasattr(os, "getpgid"):
        job.pgid = process.pid
        return
    try:
        job.pgid = os.getpgid(process.pid)
    except (ProcessLookupError, OSError):
        job.pgid = process.pid


def _is_our_own_group(pgid: int) -> bool:
    """Report whether a group id is the group this server itself sits in.

    Signalling that would take down uvicorn. It can only ever be true if
    ``start_new_session`` did nothing, which should not happen, so this is a
    seat belt and not a normal path.

    Args:
        pgid: The group id about to be signalled.

    Returns:
        True when the id is our own group, so the signal must not be sent.
    """
    if not hasattr(os, "getpgrp"):
        return False
    try:
        return pgid == os.getpgrp()
    except OSError:  # pragma: no cover - getpgrp does not fail in practice
        return False


def _signal_leftovers(job: _Job, sig: int) -> bool:
    """Signal whatever the child left behind, after the child itself has gone.

    A grandchild that outlives the child is an orphan, and in a real search it
    is Chromium. It holds two things that matter: the browser profile lock the
    next search needs, and a copy of the output pipe, which is what stops the
    reader from ever seeing the end of the output.

    The group is signalled by the id noted at launch, because the child's own
    pid has been reaped by now. That id is still ours: while a process group
    still has members, its id cannot be given to anything else. An empty group
    answers "no such process", and a group whose last member is already dying
    answers "operation not permitted" on macOS, checked on this machine. Both
    mean the same thing here, there is nothing left to stop, so neither is
    worth waking the rep over.

    Args:
        job: The job whose child has already exited.
        sig: ``signal.SIGTERM`` or ``signal.SIGKILL``.

    Returns:
        True when the signal went out, False when there was nothing left to
        signal or this platform has no process groups.
    """
    pgid = job.pgid
    if pgid is None or pgid <= 0 or not hasattr(os, "killpg"):
        return False
    if _is_our_own_group(pgid):  # pragma: no cover - only on a broken launch
        log.warning("lead job %s shares our own process group, not signalling it", job.id)
        return False
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        return False
    except OSError as exc:  # pragma: no cover - defensive
        log.warning("could not signal what lead job %s left behind: %s", job.id, exc)
        return False
    log.warning("lead job %s left something running, signalled its group %s", job.id, pgid)
    return True


def _signal_group(process: asyncio.subprocess.Process, sig: int) -> None:
    """Send one signal to the child and to everything the child started.

    The child is its own process group leader, because it is launched with
    ``start_new_session=True``. That is what makes this possible: playwright
    starts Chromium as a grandchild, so signalling only the python child would
    leave a Chromium window on screen with nobody left to close it, still
    holding the browser profile lock that the next search needs.

    Args:
        process: The child process.
        sig: ``signal.SIGTERM`` or ``signal.SIGKILL``.
    """
    pid = process.pid
    if not pid or pid <= 0:
        return
    if hasattr(os, "killpg") and hasattr(os, "getpgid"):
        try:
            group = os.getpgid(pid)
            if not _is_our_own_group(group):
                os.killpg(group, sig)
                return
        except ProcessLookupError:
            return
        except (PermissionError, OSError) as exc:
            log.warning("could not signal the process group of %s: %s", pid, exc)
    # Fallback for a platform with no process groups, or a group that is gone.
    with contextlib.suppress(ProcessLookupError, OSError):
        if sig == getattr(signal, "SIGKILL", signal.SIGTERM):
            process.kill()
        else:
            process.terminate()


async def _kill_tree(job: _Job) -> None:
    """Stop the child politely, then stop it rudely.

    SIGTERM first, so playwright gets its chance to close the browser cleanly
    and the checkpoint file stays usable for a resume. If it is still there
    after :data:`TERM_GRACE_S`, SIGKILL the group.

    The waits go through :func:`_exited_within`, not through
    ``process.wait()``, which would still be waiting on a pipe that a surviving
    Chromium holds open.

    Args:
        job: The job to stop. Doing this to a job that already exited is safe.
    """
    process = job.process
    if process is None:
        return
    if process.returncode is not None:
        # The child is already gone. Anything still alive in its group is an
        # orphan it left behind, so stop that rather than stopping nothing.
        _signal_leftovers(job, signal.SIGTERM)
        return
    _signal_group(process, signal.SIGTERM)
    if await _exited_within(process, TERM_GRACE_S):
        return
    log.warning("lead job %s ignored SIGTERM, killing it", job.id)
    _signal_group(process, getattr(signal, "SIGKILL", signal.SIGTERM))
    await _exited_within(process, KILL_GRACE_S)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


async def start_scrape(
    niche: str,
    location: str,
    limit: int = DEFAULT_LIMIT,
    country: str = DEFAULT_COUNTRY,
) -> dict[str, Any]:
    """Start a scrape in the background and return the job at once.

    Only one scrape may run at a time. When one is already running this returns
    that job with ``alreadyRunning`` set to True and starts nothing, because a
    second Chromium on the same browser profile fails and takes the first one
    down with it.

    Args:
        niche: What to search for, for example ``dentist``.
        location: Where to search, for example ``lahore``.
        limit: How many leads to ask for. Clamped to 1 up to 400.
        country: Two letter country code, for example ``pk``.

    Returns:
        The job dict, see :meth:`_Job.to_wire`. It carries ``jobId`` and
        ``searchId`` straight away, so the caller can answer the request
        without waiting for the child to print anything.

    Raises:
        InvalidSearchError: When the niche, the city, the country or the limit
            is not something that should reach a command line. It subclasses
            ValueError, and its message is safe to show the rep.
    """
    clean_niche = _clean_term(niche, "niche", "for example barber")
    clean_location = _clean_term(location, "city", "for example hoboken")
    clean_country = _clean_country(country)
    clean_limit = _clean_limit(limit)

    slug = search_id(clean_niche, clean_location)
    if not slug:
        raise InvalidSearchError("Use letters or numbers for the niche and the city.")

    async with _start_lock:
        _sweep()
        running = _running_job()
        if running is not None:
            log.info(
                "lead job %s is already running, not starting %r in %r",
                running.id,
                clean_niche,
                clean_location,
            )
            return running.to_wire(already_running=True)

        job = _Job(
            id=uuid.uuid4().hex[:12],
            niche=clean_niche,
            location=clean_location,
            limit=clean_limit,
            country=clean_country,
            search_id=slug,
        )
        _jobs[job.id] = job

        if not LEADENGINE_DIR.is_dir():
            _finish(job, None, "The lead engine folder is missing at " + str(LEADENGINE_DIR) + ".")
            return job.to_wire()

        command = _build_command(clean_niche, clean_location, clean_limit, clean_country)
        log.info("lead job %s starting: %s", job.id, " ".join(command))
        try:
            # No shell. Every value is its own argv entry, so a search term can
            # never be read as a command.
            job.process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(REPO_ROOT),
                env=_child_env(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            log.exception("lead job %s could not start", job.id)
            _finish(job, None, "The search could not start. " + str(exc))
            return job.to_wire()

        _remember_group(job)
        job.task = asyncio.create_task(_watch(job), name="lead-scrape-" + job.id)
        return job.to_wire()


def get_job(job_id: str) -> dict[str, Any] | None:
    """Return one job, or None when it is unknown or has been swept.

    This only reads memory, so it is not async. Do not await it.

    Args:
        job_id: The id handed back by :func:`start_scrape`.

    Returns:
        The job dict, or None.
    """
    _sweep()
    job = _jobs.get(job_id)
    return None if job is None else job.to_wire()


def list_jobs() -> list[dict[str, Any]]:
    """Return every job we still remember, newest first.

    This only reads memory, so it is not async. Do not await it.

    Returns:
        A list of job dicts.
    """
    _sweep()
    jobs = sorted(_jobs.values(), key=lambda item: item.started_at, reverse=True)
    return [job.to_wire() for job in jobs]


def running_job() -> dict[str, Any] | None:
    """Return the running job, or None when nothing is running.

    Handy for the leads screen, which needs to know whether to offer a new
    search at all.
    """
    _sweep()
    job = _running_job()
    return None if job is None else job.to_wire()


async def cancel_job(job_id: str) -> dict[str, Any] | None:
    """Stop a running job and everything it started.

    Kills the whole process group, so the Chromium window really closes. A job
    that has already ended is returned as it is, which makes a double click on
    the stop button harmless.

    Args:
        job_id: The id handed back by :func:`start_scrape`.

    Returns:
        The job dict after the stop, or None when the id is unknown.
    """
    _sweep()
    job = _jobs.get(job_id)
    if job is None:
        return None
    if job.state != "running":
        return job.to_wire()

    log.info("lead job %s cancelled by the rep", job.id)
    job.cancelled = True
    job.step = STEP_STOPPING
    await _kill_tree(job)

    task = job.task
    if task is not None and not task.done():
        # Shielded, so this timeout cannot cancel the watcher and leave the job
        # stuck on running for ever.
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), FINALISE_GRACE_S)

    if job.state == "running":
        # The watcher never got there. Close the job by hand so the one running
        # slot is free again.
        _finish(job, job.process.returncode if job.process is not None else None)
    return job.to_wire()


async def shutdown() -> None:
    """Stop anything still running, for the FastAPI lifespan to call.

    Without this a scrape started a minute before the server is restarted keeps
    a Chromium window open with nothing left to watch it.
    """
    for job in list(_jobs.values()):
        if job.state == "running":
            await cancel_job(job.id)


__all__ = [
    "InvalidSearchError",
    "MAX_LINES",
    "JOB_TTL_S",
    "REPO_ROOT",
    "VENV_PYTHON",
    "cancel_job",
    "get_job",
    "list_jobs",
    "running_job",
    "search_id",
    "shutdown",
    "start_scrape",
]


# --------------------------------------------------------------------------- #
# Self test
# --------------------------------------------------------------------------- #
#
# Run it with:
#
#     backend/.venv/bin/python backend/app/services/lead_jobs.py
#
# It never runs the real scraper. A real run opens a Chromium window and hits
# Google for minutes, which is not a test. Instead the same machinery, the same
# launch, the same reader, the same finish and the same kill, is pointed at a
# tiny python command that prints and sleeps.


@contextmanager
def _fake_command(script: str) -> Iterator[None]:
    """Point the launcher at a harmless python one liner for the test."""
    original = _build_command

    def fake(niche: str, location: str, limit: int, country: str) -> list[str]:
        return [str(VENV_PYTHON), "-u", "-c", script]

    globals()["_build_command"] = fake
    try:
        yield
    finally:
        globals()["_build_command"] = original


async def _wait_done(job_id: str, timeout: float = 20.0) -> dict[str, Any]:
    """Poll a job until it is done, the way the UI does."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = get_job(job_id)
        assert state is not None, "the job vanished while it was running"
        if state["done"]:
            return state
        await asyncio.sleep(0.05)
    raise AssertionError("job " + job_id + " did not finish inside " + str(timeout) + "s")


async def _test_lines_are_captured() -> None:
    script = "import time\nfor word in ('one', 'two', 'three'):\n    print(word)\n    time.sleep(0.05)\n"
    with _fake_command(script):
        started = await start_scrape("barber", "hoboken", 10, "us")
    assert started["state"] == "running", started["state"]
    assert started["searchId"] == "barber-hoboken", started["searchId"]
    assert started["done"] is False
    assert started["alreadyRunning"] is False
    done = await _wait_done(started["jobId"])
    assert done["state"] == "finished", done
    assert done["exitCode"] == 0, done["exitCode"]
    assert done["lines"] == ["one", "two", "three"], done["lines"]
    assert done["line"] == "three", done["line"]
    assert done["step"] == STEP_DONE, done["step"]
    assert done["error"] == "", done["error"]
    print("ok   lines captured, last line is 'three', state ran then finished")


async def _test_step_and_carriage_return() -> None:
    # The real engine prints a step banner, then draws a live counter with a
    # carriage return and an erase code and no newline at all. The child holds
    # on for a moment at the end so the test can read the job while it is still
    # running, which is the whole reason the output is kept.
    script = (
        "import sys, time\n"
        "print('  STEP 2/4  .  Har lead ka deep audit')\n"
        "sys.stdout.write('\\r\\x1b[K  working 1')\n"
        "sys.stdout.flush()\n"
        "time.sleep(0.05)\n"
        "sys.stdout.write('\\r\\x1b[K  working 2')\n"
        "sys.stdout.flush()\n"
        "print()\n"
        "time.sleep(1.5)\n"
    )
    with _fake_command(script):
        started = await start_scrape("car wash", "new york", 10, "us")

    live: dict[str, Any] | None = None
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        state = get_job(started["jobId"])
        assert state is not None
        if state["line"] == "working 2":
            live = state
            break
        await asyncio.sleep(0.05)
    assert live is not None, "the progress line never arrived while the job ran"
    assert live["state"] == "running", live["state"]
    assert live["done"] is False
    assert "working 1" in live["lines"], live["lines"]
    assert live["step"] == "Step 2 of 4, Checking each business", live["step"]

    done = await _wait_done(started["jobId"])
    assert done["state"] == "finished", done
    assert done["line"] == "working 2", done["line"]
    assert done["step"] == STEP_DONE, done["step"]
    print("ok   step banner and carriage return progress are read while it runs")


async def _test_failure_is_recorded() -> None:
    with _fake_command("raise SystemExit(3)\n"):
        started = await start_scrape("gym", "dubai", 10, "ae")
    done = await _wait_done(started["jobId"])
    assert done["state"] == "failed", done["state"]
    assert done["exitCode"] == 3, done["exitCode"]
    assert done["ok"] is False
    assert "3" in done["error"], done["error"]
    print("ok   a child that exits non zero is recorded as failed")


async def _is_alive(pid: int) -> bool:
    """Report whether a pid still exists, for the kill checks."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - it exists, we just cannot signal it
        return True
    return True


async def _wait_gone(pid: int, timeout: float = 5.0) -> bool:
    """Wait for a pid to disappear."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not await _is_alive(pid):
            return True
        await asyncio.sleep(0.05)
    return False


async def _test_one_at_a_time_and_cancel() -> None:
    # The child starts a grandchild and hands us its pid. That grandchild is
    # standing in for Chromium, which playwright starts the same way and which
    # also inherits this pipe. Killing only the python child would leave it
    # running with the pipe open, which is exactly the leak to guard against.
    script = (
        "import subprocess, sys, time\n"
        "kid = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print('grandchild ' + str(kid.pid), flush=True)\n"
        "time.sleep(60)\n"
    )
    with _fake_command(script):
        first = await start_scrape("dentist", "lahore", 20, "pk")
        assert first["state"] == "running", first["state"]

        second = await start_scrape("plumber", "karachi", 20, "pk")
        assert second["jobId"] == first["jobId"], "a second Chromium was started"
        assert second["alreadyRunning"] is True, second
        assert second["searchId"] == "dentist-lahore", second["searchId"]

    child = _jobs[first["jobId"]].process
    assert child is not None
    pid = child.pid

    grandchild_pid = 0
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        state = get_job(first["jobId"])
        assert state is not None
        if state["line"].startswith("grandchild "):
            grandchild_pid = int(state["line"].split()[1])
            break
        await asyncio.sleep(0.05)
    assert grandchild_pid, "the child never reported its grandchild"
    assert await _is_alive(grandchild_pid), "the grandchild died on its own"

    began = time.monotonic()
    cancelled = await cancel_job(first["jobId"])
    took = time.monotonic() - began
    assert cancelled is not None
    assert cancelled["state"] == "failed", cancelled["state"]
    assert cancelled["cancelled"] is True, cancelled
    assert cancelled["done"] is True
    assert cancelled["error"] == "You stopped this search."
    assert cancelled["step"] == STEP_STOPPED, cancelled["step"]
    assert took < 8.0, "cancel took " + str(round(took, 1)) + "s"

    assert await _wait_gone(pid), "child " + str(pid) + " is still alive after cancel"
    assert await _wait_gone(grandchild_pid), (
        "grandchild " + str(grandchild_pid) + " is still alive, this is the Chromium leak"
    )

    # Cancelling twice must not blow up, and must not change the answer.
    again = await cancel_job(first["jobId"])
    assert again is not None and again["state"] == "failed"
    assert await cancel_job("no-such-job") is None

    # The one running slot has to be free again.
    with _fake_command("print('after')\n"):
        third = await start_scrape("barber", "hoboken", 10, "us")
    assert third["jobId"] != first["jobId"], "the slot was never released"
    after = await _wait_done(third["jobId"])
    assert after["line"] == "after", after["line"]
    print("ok   one job at a time, cancel kills the child and the grandchild")


async def _test_a_dead_child_never_wedges_the_slot() -> None:
    # The nastiest real failure. The python child dies hard, on its own, while
    # the grandchild it started is still alive and still holding a copy of the
    # same output pipe. That is what a crashed scraper looks like from here:
    # playwright's Chromium keeps the pipe open, so the end of the output never
    # arrives. The job still has to end, or the one running slot is locked for
    # the rest of the day and every later search is refused.
    script = (
        "import os, subprocess, sys, time\n"
        "kid = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(90)'])\n"
        "print('grandchild ' + str(kid.pid), flush=True)\n"
        "time.sleep(0.2)\n"
        "os._exit(0)\n"
    )
    with _fake_command(script):
        started = await start_scrape("bakery", "boston", 10, "us")

    grandchild_pid = 0
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        state = get_job(started["jobId"])
        assert state is not None
        if state["line"].startswith("grandchild "):
            grandchild_pid = int(state["line"].split()[1])
            break
        await asyncio.sleep(0.05)
    assert grandchild_pid, "the child never reported its grandchild"

    done = await _wait_done(started["jobId"], timeout=30.0)
    assert done["state"] == "finished", done
    assert done["exitCode"] == 0, done["exitCode"]
    assert done["done"] is True

    # The orphan that was holding the pipe has to be gone too, because it also
    # holds the browser profile the next search needs.
    assert await _wait_gone(grandchild_pid, 10.0), (
        "orphan " + str(grandchild_pid) + " is still holding the pipe"
    )

    # And the slot has to be free, which is the whole point.
    with _fake_command("print('free')\n"):
        after = await start_scrape("barber", "hoboken", 10, "us")
    assert after["alreadyRunning"] is False, "the dead job is still holding the slot"
    assert after["jobId"] != started["jobId"], "the dead job was handed back"
    finished = await _wait_done(after["jobId"])
    assert finished["line"] == "free", finished["line"]
    print("ok   a child that dies leaving a grandchild still ends, and frees the slot")


async def _test_bad_input_is_refused() -> None:
    before = len(_jobs)
    bad: list[tuple[str, str, object, object]] = [
        ("", "hoboken", 10, "us"),
        ("   ", "hoboken", 10, "us"),
        ("barber", "", 10, "us"),
        ("b" * 200, "hoboken", 10, "us"),
        ("barber", "h" * 200, 10, "us"),
        ("-rf", "hoboken", 10, "us"),
        ("!!!", "hoboken", 10, "us"),
        ("barber", "hoboken", 10, "usa"),
        ("barber", "hoboken", 10, "1"),
        ("barber", "hoboken", "many", "us"),
    ]
    for niche, location, limit, country in bad:
        try:
            await start_scrape(niche, location, limit, country)  # type: ignore[arg-type]
        except InvalidSearchError as exc:
            assert isinstance(exc, ValueError)
            assert str(exc), "the message shown to the rep is empty"
            # The long dash is banned in every string the rep can see.
            assert "\u2014" not in str(exc)
        else:
            raise AssertionError("bad input was accepted: " + repr((niche, location)))
    assert len(_jobs) == before, "a refused search still made a job"

    # A shell would read this as two commands. Argv never does, so it is only a
    # long search term, and the slug proves it stayed one value.
    assert search_id("barber; rm -rf ~", "hoboken") == "barber-rm-rf-hoboken"
    assert _clean_limit(100000) == MAX_LIMIT
    assert _clean_limit(0) == MIN_LIMIT
    assert _clean_limit(None) == DEFAULT_LIMIT
    assert _clean_country("") == DEFAULT_COUNTRY
    assert _clean_country(" PK ") == "pk"
    print("ok   empty, long, dash leading and odd values are refused before launch")


async def _test_sweep_keeps_an_hour() -> None:
    now = time.time()
    old = _Job(
        id="oldjob",
        niche="barber",
        location="hoboken",
        limit=10,
        country="us",
        search_id="barber-hoboken",
        state="finished",
        started_at=now - 4000.0,
        finished_at=now - 3700.0,
    )
    fresh = _Job(
        id="freshjob",
        niche="barber",
        location="hoboken",
        limit=10,
        country="us",
        search_id="barber-hoboken",
        state="finished",
        started_at=now - 40.0,
        finished_at=now - 10.0,
    )
    _jobs[old.id] = old
    _jobs[fresh.id] = fresh
    _sweep()
    assert "oldjob" not in _jobs, "a job older than an hour was kept"
    assert "freshjob" in _jobs, "a job from ten seconds ago was swept"
    assert get_job("oldjob") is None
    assert get_job("freshjob") is not None
    _jobs.pop("freshjob", None)
    print("ok   jobs are kept for an hour and older ones are swept")


async def _test_real_paths() -> None:
    assert REPO_ROOT.name == "AI Calling Sys", str(REPO_ROOT)
    assert (REPO_ROOT / "leadengine" / "run.py").is_file(), "leadengine/run.py is missing"
    assert VENV_PYTHON.exists(), "the venv python is missing at " + str(VENV_PYTHON)
    # The slug has to match the files the rep already has on disk, or the UI
    # would select a search that does not exist.
    assert search_id("barber", "hoboken") == "barber-hoboken"
    assert search_id("car wash", "new york") == "car-wash-new-york"
    assert search_id("dentist", "jersey city") == "dentist-jersey-city"
    for slug in ("barber-hoboken", "car-wash-new-york", "dentist-jersey-city"):
        path = REPO_ROOT / "leadengine" / "data" / (slug + ".ndjson")
        assert path.is_file(), "real data file is missing: " + str(path)
    command = _build_command("car wash", "new york", 60, "us")
    assert command[0] == str(VENV_PYTHON)
    assert command[1:4] == ["-u", "-m", "leadengine.run"]
    assert command[4] == "car wash", "the niche was split into two argv entries"
    assert command[5] == "new york"
    assert command[6:] == ["--limit", "60", "--country", "us"]
    print("ok   paths, slugs and the command line match the real repo and data")


async def _main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s | %(message)s")
    tests = [
        _test_real_paths,
        _test_lines_are_captured,
        _test_step_and_carriage_return,
        _test_failure_is_recorded,
        _test_one_at_a_time_and_cancel,
        _test_a_dead_child_never_wedges_the_slot,
        _test_bad_input_is_refused,
        _test_sweep_keeps_an_hour,
    ]
    for test in tests:
        await test()
    await shutdown()
    assert _running_job() is None, "something is still running at the end"
    print()
    print("all " + str(len(tests)) + " lead_jobs self tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
