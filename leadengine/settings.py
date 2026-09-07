"""Shared paths and tunables for the lead engine.

Har cheez yahan se aati hai - koi module apne raste khud nahi banata.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

# This file lives inside the package, so the package directory is simply its own
# parent. Deriving it that way rather than naming the folder means moving or
# renaming the package cannot silently point the data directory somewhere empty,
# which is what happened when it was called "worker".
WORKER = Path(__file__).resolve().parent
ROOT = WORKER.parent
CONFIG_DIR = WORKER / "config"
DATA_DIR = WORKER / "data"
SCREENSHOT_DIR = DATA_DIR / "screenshots"
PROFILE_DIR = WORKER / ".chrome-profile"
PROMPTS_DIR = WORKER / "prompts"

for _d in (DATA_DIR, SCREENSHOT_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def load_config(name: str) -> dict:
    """Load a JSON file from leadengine/config/."""
    return json.loads((CONFIG_DIR / name).read_text(encoding="utf-8"))


@dataclass(frozen=True)
class ScrapeSettings:
    """Scraper knobs. Env vars override so nothing needs a code edit."""

    headless: bool = os.getenv("GMO_HEADLESS", "0") == "1"
    locale: str = "en-US"
    viewport_width: int = 1440
    viewport_height: int = 900
    # Human-ish pacing between scrolls / detail visits (seconds).
    min_delay: float = 0.8
    max_delay: float = 2.5
    # Safety caps so a runaway search can't scroll forever.
    max_listings: int = int(os.getenv("GMO_MAX_LISTINGS", "400"))
    max_scroll_minutes: float = float(os.getenv("GMO_MAX_SCROLL_MINUTES", "15"))
    # Feed height must stay unchanged this many rounds before we call it done.
    stable_rounds_to_stop: int = 3
    nav_timeout_ms: int = 45_000
    detail_timeout_ms: int = 20_000


SCRAPE = ScrapeSettings()

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
