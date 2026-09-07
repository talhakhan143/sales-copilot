"""Ek command — poora pipeline.

    python -m leadengine.run "car wash" "new york"
    python -m leadengine.run "dentist" "lahore" --limit 60 --country pk --language roman_urdu

Chaar stage chalti hain: scrape -> audit -> score -> messages.
Har stage checkpointed hai, is liye beech me ruk jaye to dobara chalane pe
wahin se chalti hai.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from leadengine.settings import DATA_DIR

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

RESET, DIM, BOLD = "\033[0m", "\033[2m", "\033[1m"
GREEN, YELLOW, CYAN, MAGENTA = "\033[32m", "\033[33m", "\033[36m", "\033[35m"


def paint(text: str, code: str) -> str:
    return code + text + RESET if sys.stdout.isatty() else text


def banner(step: int, total: int, title: str) -> None:
    print()
    print(paint("━" * 70, DIM))
    print(paint(BOLD + "  STEP " + str(step) + "/" + str(total) + "  ·  " + title + RESET, CYAN))
    print(paint("━" * 70, DIM))


def money_str(symbol: str, amount: int) -> str:
    if amount >= 1000:
        return symbol + str(round(amount / 1000, 1)).rstrip("0").rstrip(".") + "k"
    return symbol + str(amount)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m leadengine.run",
        description="Scrape -> audit -> score -> messages, sab ek saath.",
    )
    parser.add_argument("niche", help='e.g. "car wash"')
    parser.add_argument("location", help='e.g. "new york"')
    parser.add_argument("--limit", type=int, default=60)
    parser.add_argument("--country", default="us")
    parser.add_argument("--language", default="en", choices=["en", "roman_urdu", "urdu"])
    parser.add_argument("--top", type=int, default=25, help="kitne top leads ke messages banein")
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--fast", action="store_true", help="scrape me detail visits skip")
    parser.add_argument("--fresh", action="store_true", help="sab kuch naye sire se")
    parser.add_argument("--skip-messages", action="store_true")
    args = parser.parse_args(argv)

    from leadengine.scrape import search_id

    sid = search_id(args.niche, args.location)
    started = time.time()

    print(paint(BOLD + '\n  "' + args.niche + '" in "' + args.location + '"' + RESET, MAGENTA))
    print(paint("  search-id: " + sid, DIM))

    total_steps = 3 if args.skip_messages else 4

    # ---- 1. scrape -------------------------------------------------------
    banner(1, total_steps, "Google Maps se leads")
    from leadengine import scrape as scrape_cli

    scrape_args = [args.niche, args.location, "--limit", str(args.limit), "--country", args.country]
    if args.fast:
        scrape_args.append("--fast")
    if args.fresh:
        scrape_args.append("--fresh")
    scrape_cli.main(scrape_args)

    leads_path = DATA_DIR / (sid + ".ndjson")
    if not leads_path.exists() or not leads_path.read_text(encoding="utf-8").strip():
        print(paint("\n  Koi lead nahi mili — niche ya location badal ke dekho.", YELLOW))
        return 1

    # ---- 2. audit --------------------------------------------------------
    banner(2, total_steps, "Har lead ka deep audit")
    from leadengine.audit.__main__ import main as audit_main

    audit_args = [sid, "--concurrency", str(args.concurrency)]
    if args.fresh:
        audit_args.append("--fresh")
    audit_main(audit_args)

    # ---- 3. score --------------------------------------------------------
    banner(3, total_steps, "Score, deal value aur pain points")
    from leadengine.scoring.__main__ import main as scoring_main

    scoring_main([sid])

    # ---- 4. messages -----------------------------------------------------
    if not args.skip_messages:
        banner(4, total_steps, "Email + Instagram DM + SMS")
        from leadengine.messages.__main__ import main as messages_main

        msg_args = [sid, "--language", args.language, "--top", str(args.top)]
        if args.fresh:
            msg_args.append("--fresh")
        messages_main(msg_args)

    # ---- summary ---------------------------------------------------------
    scores = json.loads((DATA_DIR / (sid + ".scores.json")).read_text(encoding="utf-8"))
    records = list(scores["leads"].values())
    symbol = records[0]["money"]["currency_symbol"] if records else "$"
    pipeline = sum(r["money"]["contract_value_usd"] for r in records)
    weighted = sum(r["money"]["expected_value_usd"] for r in records)
    website_deals = sum(1 for r in records if r["money"]["deal_type"] == "WEBSITE")
    elapsed = int(time.time() - started)

    print()
    print(paint("═" * 70, DIM))
    print(paint(BOLD + "  HO GAYA" + RESET, GREEN) + paint("   " + str(elapsed) + "s me", DIM))
    print()
    print("  " + str(len(records)).rjust(4) + paint("  leads", DIM))
    print("  " + str(website_deals).rjust(4) + paint("  website bechni hai", DIM))
    print("  " + str(len(records) - website_deals).rjust(4) + paint("  SEO bechni hai", DIM))
    print()
    print("  " + paint("Total pipeline value:  ", DIM) + paint(money_str(symbol, pipeline), MAGENTA))
    print("  " + paint("Realistic (close-rate lagane ke baad): ", DIM) + money_str(symbol, weighted))
    print()
    print(paint("  Files:", DIM))
    for suffix in (".ndjson", ".audit.json", ".scores.json", ".messages.json"):
        path = DATA_DIR / (sid + suffix)
        if path.exists():
            print(paint("    " + path.name, DIM))
    print(paint("═" * 70, DIM))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
