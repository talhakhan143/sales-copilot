"""CLI: Google Maps se leads scrape karo.

    python -m leadengine.scrape "car wash" "new york"
    python -m leadengine.scrape "dentist" "lahore" --limit 100 --country pk
    python -m leadengine.scrape "gym" "dubai" --fast        # detail visits skip
    python -m leadengine.scrape "car wash" "new york" --fresh  # checkpoint reset

Output: worker/data/<search-id>.ndjson  (har line ek lead)
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

from leadengine.scraper.maps import Checkpoint, MapsScraper
from leadengine.settings import DATA_DIR

# Windows console cp1252 hoti hai - arrows/box chars crash kar dete hain.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

# ---------------------------------------------------------------- pretty out

RESET, DIM, BOLD = "\033[0m", "\033[2m", "\033[1m"
GREEN, YELLOW, RED, CYAN = "\033[32m", "\033[33m", "\033[31m", "\033[36m"


def supports_color() -> bool:
    return sys.stdout.isatty()


def paint(text: str, code: str) -> str:
    return code + text + RESET if supports_color() else text


def search_id(niche: str, location: str) -> str:
    """Stable slug so a re-run resumes the same checkpoint file."""
    slug = re.sub(r"[^a-z0-9]+", "-", (niche + "--" + location).lower()).strip("-")
    return slug[:80]


class ConsoleProgress:
    """Ek hi line pe live counter, warnings alag."""

    def __init__(self) -> None:
        self.leads = 0
        self.with_site = 0
        self.without_site = 0
        self.with_phone = 0
        self.warnings: list[str] = []
        self.started = time.time()

    def __call__(self, stage: str, info: dict) -> None:
        if stage == "open":
            print(paint("→ Kholte hain: ", CYAN) + info["url"])
        elif stage == "scroll":
            self._line("Scrolling… " + str(info["found"]) + " listings mile")
        elif stage == "scroll_end":
            print()
            print(paint("✓ Scroll khatam", GREEN) + paint("  (" + info["reason"] + ")", DIM))
        elif stage == "collected":
            print(paint("✓ " + str(info["count"]) + " listings collect huin", GREEN))
            print(paint("  Ab har ek ki detail khol rahe hain…", DIM))
        elif stage == "skip":
            self._line("Skip (pehle se hai): " + info["name"][:40])
        elif stage == "lead":
            lead = info["lead"]
            self.leads += 1
            if lead.website:
                self.with_site += 1
            else:
                self.without_site += 1
            if lead.phone:
                self.with_phone += 1
            badge = "SEO " if lead.website else "SITE"
            self._line(
                "["
                + str(info["index"])
                + "/"
                + str(info["total"])
                + "] "
                + badge
                + "  "
                + lead.name[:44]
            )
        elif stage == "warn":
            self.warnings.append(info["message"])

    def _line(self, text: str) -> None:
        sys.stdout.write("\r\033[K  " + text)
        sys.stdout.flush()

    def summary(self, path: Path) -> None:
        elapsed = int(time.time() - self.started)
        print("\r\033[K")
        print(paint("─" * 58, DIM))
        print(paint(BOLD + "  " + str(self.leads) + " leads scraped" + RESET, GREEN))
        print("  " + paint("Website nahi (WEBSITE bechna):", DIM) + " " + str(self.without_site))
        print("  " + paint("Website hai   (SEO bechna):   ", DIM) + " " + str(self.with_site))
        print("  " + paint("Phone mila:                   ", DIM) + " " + str(self.with_phone))
        print("  " + paint("Time:                         ", DIM) + " " + str(elapsed) + "s")
        print("  " + paint("File:                         ", DIM) + " " + str(path))
        if self.warnings:
            print()
            print(paint("  " + str(len(self.warnings)) + " warnings:", YELLOW))
            for msg in self.warnings[:8]:
                print(paint("    · " + msg[:90], DIM))
        print(paint("─" * 58, DIM))


# ---------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m leadengine.scrape",
        description="Google Maps se businesses scrape karo (koi API nahi).",
    )
    parser.add_argument("niche", help='e.g. "car wash"')
    parser.add_argument("location", help='e.g. "new york"')
    parser.add_argument("--limit", type=int, default=None, help="kitne listings (default 400)")
    parser.add_argument("--country", default="us", help="gl code: us, gb, pk, ae …")
    parser.add_argument(
        "--fast", action="store_true", help="detail visits skip (phone/hours nahi milenge)"
    )
    parser.add_argument("--headless", action="store_true", help="browser chhupa ke chalao")
    parser.add_argument("--fresh", action="store_true", help="purana checkpoint delete karo")
    parser.add_argument("--out", default=None, help="output NDJSON path")
    args = parser.parse_args(argv)

    if args.headless:
        import os

        os.environ["GMO_HEADLESS"] = "1"

    sid = search_id(args.niche, args.location)
    out_path = Path(args.out) if args.out else DATA_DIR / (sid + ".ndjson")
    if args.fresh and out_path.exists():
        out_path.unlink()

    checkpoint = Checkpoint(out_path)
    if checkpoint.seen:
        print(
            paint(
                "↻ Resume: " + str(len(checkpoint.seen)) + " leads pehle se hain, skip honge",
                YELLOW,
            )
        )

    print(paint(BOLD + '\n  "' + args.niche + '" in "' + args.location + '"' + RESET, CYAN))
    print(paint("  " + ("fast mode" if args.fast else "deep mode") + " · " + sid, DIM))
    print()

    progress = ConsoleProgress()
    scraper = MapsScraper(progress=progress)
    try:
        for _ in scraper.scrape(
            niche=args.niche,
            location=args.location,
            limit=args.limit,
            deep=not args.fast,
            checkpoint=checkpoint,
            country=args.country,
        ):
            pass
    except KeyboardInterrupt:
        print("\n" + paint("  Rok diya. Checkpoint safe hai - dobara chalao to resume hoga.", YELLOW))

    progress.summary(out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
