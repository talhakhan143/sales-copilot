"""Application settings loaded from the environment and from a local .env file.

Field names are snake_case on the Python side and map to the UPPER_SNAKE
environment variable of the same name, because ``case_sensitive`` is off. So
``groq_api_key`` reads ``GROQ_API_KEY``, ``vad_silence_ms`` reads
``VAD_SILENCE_MS``, and so on.

Nothing in this module performs IO beyond reading the process environment and
the .env file at import time, so it is safe to import from anywhere including
the event loop.

Typical use:
    from app.config import settings, cors_origin_list

    origins = cors_origin_list()
    if not settings.groq_api_key:
        log.warning("no GROQ key")
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger("salescopilot.config")


def _repo_root() -> Path:
    """Return the directory that holds ``backend/`` and ``leadengine/``.

    Worked out from this file rather than from the working directory, because
    the backend is started from ``backend/`` by ``run.sh``, from the repo root
    by ``start.sh`` and from anywhere at all by an editor. A relative default
    would point at a different, usually empty, folder in each of those cases.

    Returns:
        The repository root, which is two levels above ``app/config.py``.
    """
    return Path(__file__).resolve().parent.parent.parent


def _default_leadengine_data_dir() -> str:
    """Return the lead engine data directory that ships with the repo.

    Returns:
        The absolute path of ``leadengine/data`` as a string, so the value can
        be overridden by a plain ``LEADENGINE_DATA_DIR`` environment variable.
    """
    return str(_repo_root() / "leadengine" / "data")


class Settings(BaseSettings):
    """Runtime configuration for the Sales Copilot backend.

    Every field has a safe default so the application still boots with an empty
    environment. The only value that really matters for a working install is
    ``groq_api_key``. When it is empty the API still serves, prepares context
    and accepts WebSocket connections, but transcription and suggestions fail
    with a clear error instead of silently doing nothing.

    The calling fields are all optional in the same way. With none of them set
    the app still runs, and the call provider list simply reports Twilio and
    WhatsApp Cloud as not ready, naming the variables that are still missing.

    Attributes:
        groq_api_key: Groq API key. Empty means the Groq client is not
            configured and STT plus LLM calls will be rejected early.
        groq_base_url: OpenAI compatible base URL for the Groq API.
        stt_model: Whisper model id used for speech to text.
        llm_model: Chat completion model id used for suggestions.
        jina_base_url: Jina Reader prefix. The target URL is appended to it.
        jina_api_key: Optional Jina key. It only raises the rate limit, the
            reader works without it.
        cors_origins: Comma separated list of allowed browser origins.
        session_ttl_seconds: Age after which an idle session is swept.
        max_scrape_chars: Hard cap on characters kept from a scraped page.
        max_kb_chars: Hard cap on characters kept from the pasted knowledge base.
        transcript_window_turns: How many recent turns are replayed to the LLM.
        llm_max_tokens: Token ceiling for a single suggestion.
        llm_temperature: Sampling temperature for suggestions.
        vad_silence_ms: Trailing silence that closes an utterance.
        vad_min_speech_ms: Utterances shorter than this are discarded.
        vad_max_utterance_ms: Forced cut length for a long monologue.
        vad_preroll_ms: Audio kept before speech onset so word starts survive.
        log_level: Root logging level name, for example INFO or DEBUG.
        twilio_account_sid: Twilio account id, the ``AC...`` string. It is the
            user half of the HTTP basic auth pair.
        twilio_auth_token: Twilio auth token, the password half of that pair.
            A secret, never logged and never returned by :func:`redacted`.
        twilio_from_number: The E.164 number rented inside the Twilio console.
            Twilio refuses to place a call from a number the account does not
            own, so this is not a free choice.
        public_base_url: The address at which the public internet can reach
            this backend, for example ``https://abc123.ngrok-free.app``. Twilio
            fetches the TwiML and opens the media socket from outside, so a
            localhost address is useless to it. It has to carry ``http://`` or
            ``https://`` in front, because the webhook address is built by
            joining this value with a path. See :func:`public_http_base` for
            the strict check and :func:`public_wss_base` for the socket URL.
        whatsapp_token: Meta Graph API access token for WhatsApp Cloud calling.
            A secret. Empty means only the free link mode is available.
        whatsapp_phone_id: The WhatsApp Business phone number id from the Meta
            dashboard. It is an id, not a phone number.
        whatsapp_api_version: Graph API version prefix used in the call URL.
        call_webhook_secret: Key for the short token in the Twilio webhook URLs.
            Empty is allowed. The caller then builds a key out of values this
            machine can work out again, so a token stays valid even when the
            server restarts in the middle of a call. Set this to a real secret
            of your own before anyone else can reach the server.
        default_country_code: Digits of the country code, for example ``92``,
            used only to read a local number such as ``0300 1234567``. Empty
            means a number without a country code is rejected instead of
            guessed, which is the safe behaviour.
        leadengine_data_dir: Folder the app READS lead files from, the
            ``<slug>.ndjson``, ``<slug>.audit.json``, ``<slug>.scores.json``,
            ``<slug>.messages.json`` and ``pipeline.json`` files. The default is
            the ``leadengine/data`` folder inside this repo, worked out from
            this file, so the leads screen works with nothing set at all.

            This moves the reader only. The scraper writes where its own
            package says, which is always ``leadengine/data`` inside this repo,
            because ``leadengine/settings.py`` builds that path from its own
            file location and the merge is not allowed to edit it. So point this
            somewhere else only for a folder the rep fills some other way, for
            example their own copy of the lead engine running on its own. Do it
            and the New search button on the leads screen still runs, but its
            files land in the repo folder and this screen will not list them.
            Leave it unset and the reader and the writer are the same folder.

            Read it through :func:`leadengine_data_path`, never by hand, so
            every caller gets the same folder and the same creation rule.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    groq_api_key: str = ""
    groq_base_url: str = "https://api.groq.com/openai/v1"
    stt_model: str = "whisper-large-v3-turbo"
    llm_model: str = "qwen/qwen3.6-27b"

    jina_base_url: str = "https://r.jina.ai/"
    jina_api_key: str = ""

    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

    session_ttl_seconds: int = 21600
    max_scrape_chars: int = 12000
    max_kb_chars: int = 24000
    transcript_window_turns: int = 14

    llm_max_tokens: int = 160
    llm_temperature: float = 0.4

    vad_silence_ms: int = 620
    vad_min_speech_ms: int = 260
    vad_max_utterance_ms: int = 12000
    vad_preroll_ms: int = 320

    log_level: str = "INFO"

    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""

    public_base_url: str = ""

    whatsapp_token: str = ""
    whatsapp_phone_id: str = ""
    whatsapp_api_version: str = "v21.0"

    call_webhook_secret: str = ""

    default_country_code: str = ""

    leadengine_data_dir: str = Field(default_factory=_default_leadengine_data_dir)


settings = Settings()
"""Process wide settings singleton. Import this, do not build a second one."""


#: Hosts that only exist inside this machine. Twilio dials in from its own data
#: centre, so a URL pointing at any of these can never be fetched by it. The
#: honest answer is to report no public base at all rather than hand Twilio an
#: address that will time out.
#:
#: The list is kept the same as the one in ``app.telephony``, which is what the
#: provider picker reads. The two must not disagree, or the startup banner says
#: Twilio is ready while the picker draws it switched off. ``app.telephony``
#: imports this module, so it cannot be imported back from here, which is why
#: the same names are written out in both places.
_LOCAL_HOSTS: Final[frozenset[str]] = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "127.0.0.1",
        "0.0.0.0",
        "::1",
        "[::1]",
        "ip6-localhost",
        "host.docker.internal",
    }
)

#: Host endings that only mean something on this machine or this office network.
_LOCAL_SUFFIXES: Final[tuple[str, ...]] = (".local", ".localhost", ".internal", ".lan")


def _is_local_host(host: str) -> bool:
    """Report whether a host name can only be reached from this machine.

    Args:
        host: A bare host name or IP address, with no scheme and no port. IPv6
            brackets are removed here, so either shape may be passed in.

    Returns:
        True when Twilio could never fetch a URL on this host.
    """
    clean = host.strip().lower().strip("[]")
    if not clean or clean in _LOCAL_HOSTS:
        return True
    return clean.endswith(_LOCAL_SUFFIXES)


def cors_origin_list() -> list[str]:
    """Split the configured CORS origins into a clean list.

    The ``CORS_ORIGINS`` value is a single comma separated string so it stays
    easy to set in a .env file or a container environment. This helper turns it
    into the list shape that Starlette's CORSMiddleware expects, dropping empty
    entries and surrounding whitespace.

    Returns:
        A list of origin strings. An empty list when nothing is configured,
        which means the middleware will allow no cross origin browser calls.
    """
    return [item.strip() for item in settings.cors_origins.split(",") if item.strip()]


def twilio_ready() -> bool:
    """Report whether a real Twilio call can be attempted right now.

    All four parts have to be present. The three credentials place the call,
    and a public base URL is what lets Twilio fetch the TwiML and open the
    media socket back to us. Three out of four is not "almost ready", it is a
    call that fails after the rep has already been charged for a dial attempt.

    The address is checked with :func:`public_http_base`, the strict rule, and
    not with :func:`public_wss_base`. That matters. ``public_wss_base`` fills in
    a missing scheme for the socket URL, so a bare host such as
    ``abc123.ngrok-free.app`` passes it. The provider picker uses the strict
    rule, so the kinder one here made the startup banner print "Twilio call:
    ready" while the picker drew Twilio switched off, and the webhook address
    handed to Twilio came out with no scheme in front of it. One rule, one
    answer, everywhere.

    Returns:
        True when the account id, the auth token, the from number and a public
        base URL Twilio can really fetch are all set.
    """
    return bool(
        settings.twilio_account_sid.strip()
        and settings.twilio_auth_token.strip()
        and settings.twilio_from_number.strip()
        and public_http_base()
    )


def whatsapp_cloud_ready() -> bool:
    """Report whether the WhatsApp Cloud calling API can be attempted.

    This says nothing about Meta having approved the business number. It only
    says the two values the request needs are present. The free link mode does
    not go through here at all and always works.

    Returns:
        True when both the access token and the phone number id are set.
    """
    return bool(settings.whatsapp_token.strip() and settings.whatsapp_phone_id.strip())


def public_wss_base() -> str:
    """Turn ``PUBLIC_BASE_URL`` into the WebSocket base Twilio should dial.

    The rules are small and strict:

    * ``https://`` becomes ``wss://`` and ``http://`` becomes ``ws://``.
    * A value that is already ``wss://`` or ``ws://`` is kept as it is.
    * A bare host with no scheme is treated as ``wss://``, because a tunnel
      address is always TLS.
    * Any trailing slash is removed, so callers can append ``/ws/twilio``.
    * A missing value, or one pointing at a host only this machine can reach,
      returns an empty string. That covers localhost, the loopback addresses
      and names ending in ``.local``, ``.localhost``, ``.internal`` or
      ``.lan``, see :func:`_is_local_host`. Twilio cannot reach any of those,
      so pretending otherwise only moves the failure to the middle of a paid
      call.

    This is the kinder of the two address checks. :func:`public_http_base` is
    the strict one, and it is the one readiness is judged on.

    Returns:
        The WebSocket base such as ``wss://abc123.ngrok-free.app``, or an empty
        string when there is no address the public internet can reach.
    """
    raw = settings.public_base_url.strip()
    if not raw:
        return ""

    lowered = raw.lower()
    if lowered.startswith("https://"):
        scheme, rest = "wss://", raw[8:]
    elif lowered.startswith("http://"):
        scheme, rest = "ws://", raw[7:]
    elif lowered.startswith("wss://"):
        scheme, rest = "wss://", raw[6:]
    elif lowered.startswith("ws://"):
        scheme, rest = "ws://", raw[5:]
    elif "://" in raw:
        # Some other scheme entirely (ftp, file, a typo). We will not guess.
        return ""
    else:
        scheme, rest = "wss://", raw

    # The trailing slash goes only after the scheme has been read, so a value
    # that is nothing but a scheme ends here instead of becoming a fake host.
    rest = rest.strip().rstrip("/")
    if not rest:
        return ""

    authority = rest.split("/", 1)[0].split("@")[-1]
    if authority.startswith("["):
        host = authority[1:].split("]", 1)[0]
    else:
        host = authority.split(":", 1)[0]
    host = host.strip().lower()

    if _is_local_host(host):
        return ""

    return f"{scheme}{rest}"


def public_http_base() -> str:
    """Return the address Twilio will fetch our webhooks from, or nothing.

    This is the strict reading of ``PUBLIC_BASE_URL``, and it is the one that
    decides whether the rep is shown a button that spends money:

    * The scheme has to be ``http://`` or ``https://``. Nothing is filled in
      here. The webhook address is built by gluing a path onto this value and
      posting it to Twilio as a form field, and a value with no scheme is not
      an address, it is a word.
    * The host has to be one the public internet can reach, so localhost, the
      loopback addresses and names ending in ``.local``, ``.localhost``,
      ``.internal`` or ``.lan`` all count as missing.
    * Any trailing slash is removed, so callers can append a path.

    ``app.telephony.public_base_usable`` applies the very same rule for the
    provider picker. Keep the two the same. When they drift, the banner and the
    picker tell the rep two different stories about the same setting.

    Returns:
        The base URL such as ``https://abc123.ngrok-free.app``, or an empty
        string when the value is missing or Twilio could not fetch it.
    """
    raw = settings.public_base_url.strip()
    if not raw:
        return ""
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https"):
        return ""
    host = (parts.hostname or "").strip().lower()
    if not host or _is_local_host(host):
        return ""
    return raw.rstrip("/")


def leadengine_data_path() -> Path:
    """Return the lead engine data folder, making it when it is not there yet.

    Every reader of the lead files goes through here, so there is one answer to
    "where is the data" and one place that creates the folder. A fresh clone has
    no ``leadengine/data`` until the scraper has run once, and the leads screen
    should show an empty list with a "start a search" message rather than an
    error, so the folder is made on first use.

    Creation is best effort. A read only disk, or a path the process may not
    write to, is logged once at warning level and the path is still returned.
    The readers treat a missing folder exactly like an empty one, so the screen
    stays usable either way.

    Returns:
        The absolute path of the data folder. It may not exist if creating it
        failed, so callers still check before they read.
    """
    path = Path(settings.leadengine_data_dir).expanduser()
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning("Could not create the lead data folder %s, %s", path, exc)
    return path


def leadengine_writer_path() -> Path:
    """Return the folder a new search writes its files into.

    This one is not a setting and cannot be moved. ``leadengine/settings.py``
    builds its data path from its own file location, and the merge is not
    allowed to change the lead engine's own code, so the scraper always writes
    into ``leadengine/data`` inside this repo.

    It is here so the reader and the writer can be compared in one line, at
    startup, and an operator who moved :data:`Settings.leadengine_data_dir` is
    told that new searches will not appear on their screen instead of finding
    out after a four minute scrape.

    Returns:
        The absolute path the scraper writes into. Nothing is created here.
    """
    return Path(_default_leadengine_data_dir())


def leadengine_dirs_agree() -> bool:
    """Report whether the folder we read is the folder a new search writes into.

    Returns:
        True when a search started from this app will show up on the leads
        screen. False when the two paths were pointed at different folders.
    """
    try:
        reader = Path(settings.leadengine_data_dir).expanduser().resolve()
        writer = leadengine_writer_path().resolve()
    except OSError:
        # A path we cannot resolve, for example a broken symlink, is compared
        # as written instead. Saying "cannot tell" is not an option here, the
        # banner needs one word.
        return str(settings.leadengine_data_dir).strip() == str(leadengine_writer_path())
    return reader == writer


def _mask(secret: str) -> str:
    """Describe a secret without ever revealing it.

    Args:
        secret: The raw secret value, possibly empty.

    Returns:
        The literal string ``"set"`` when the secret has content, otherwise
        ``"not set"``. The value itself is never included, not even a prefix,
        because this output ends up in logs and in the health endpoint.
    """
    return "set" if secret.strip() else "not set"


def redacted() -> dict[str, object]:
    """Return the current settings as a plain dict with secrets masked.

    Useful for the startup banner and for a debug view on the health endpoint.
    API keys, the Twilio pair, the WhatsApp token and the webhook secret are
    replaced by ``"set"`` or ``"not set"``, and a few extra boolean keys are
    added so callers can branch without parsing strings.

    The Twilio account id is masked as well. On its own it is only an id, but it
    is the user half of the basic auth pair, so it does not belong in a page
    anyone can open.

    Returns:
        A JSON safe dict of every setting. No value in it is a secret.
    """
    data: dict[str, object] = settings.model_dump()
    data["groq_api_key"] = _mask(settings.groq_api_key)
    data["jina_api_key"] = _mask(settings.jina_api_key)
    data["twilio_account_sid"] = _mask(settings.twilio_account_sid)
    data["twilio_auth_token"] = _mask(settings.twilio_auth_token)
    data["whatsapp_token"] = _mask(settings.whatsapp_token)
    data["call_webhook_secret"] = _mask(settings.call_webhook_secret)
    data["groq_configured"] = bool(settings.groq_api_key.strip())
    data["jina_configured"] = bool(settings.jina_api_key.strip())
    data["twilio_ready"] = twilio_ready()
    data["whatsapp_cloud_ready"] = whatsapp_cloud_ready()
    data["public_wss_base"] = public_wss_base()
    data["public_http_base"] = public_http_base()
    data["cors_origins_parsed"] = cors_origin_list()

    # Resolved by hand rather than through leadengine_data_path, because that
    # helper creates the folder and a debug view must never change the disk.
    lead_dir = Path(settings.leadengine_data_dir).expanduser()
    data["leadengine_data_dir"] = str(lead_dir)
    data["leadengine_data_exists"] = lead_dir.is_dir()
    return data


__all__ = [
    "Settings",
    "settings",
    "cors_origin_list",
    "twilio_ready",
    "whatsapp_cloud_ready",
    "public_wss_base",
    "public_http_base",
    "leadengine_data_path",
    "leadengine_writer_path",
    "leadengine_dirs_agree",
    "redacted",
]
