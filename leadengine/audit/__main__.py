"""CLI: scraped leads pe audit chalao.

    python -m leadengine.audit car-wash-new-york
    python -m leadengine.audit car-wash-new-york --limit 10 --concurrency 4

Input : worker/data/<search-id>.ndjson       (scraper ka output)
Output: worker/data/<search-id>.audit.json   (feature_id -> audit)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright

from leadengine.audit.gmb import TRACK_SEO, audit_gmb
from leadengine.audit.site import audit_many
from leadengine.settings import DATA_DIR

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

RESET, DIM, BOLD = "\033[0m", "\033[2m", "\033[1m"
GREEN, YELLOW, CYAN, RED = "\033[32m", "\033[33m", "\033[36m", "\033[31m"


def paint(text: str, code: str) -> str:
    return code + text + RESET if sys.stdout.isatty() else text


def resolve_input(target: str) -> Path:
    path = Path(target)
    if path.exists():
        return path
    candidate = DATA_DIR / (target + ".ndjson")
    if candidate.exists():
        return candidate
    raise SystemExit("Input file nahi mila: " + target)


def load_leads(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def score_colour(score: int) -> str:
    return RED if score < 50 else (YELLOW if score < 80 else GREEN)


async def run(args) -> int:
    src = resolve_input(args.target)
    leads = load_leads(src)
    if args.limit:
        leads = leads[: args.limit]

    print(paint(BOLD + "\n  Audit: " + src.stem + RESET, CYAN))
    print(paint("  " + str(len(leads)) + " leads · concurrency " + str(args.concurrency), DIM))
    print()

    audits: dict[str, dict] = {}
    out_path = src.with_suffix(".audit.json")
    if out_path.exists() and not args.fresh:
        audits = json.loads(out_path.read_text(encoding="utf-8"))
        if audits:
            print(paint("  ↻ Resume: " + str(len(audits)) + " audits pehle se hain", YELLOW))

    # Step 1 - GMB audit sab pe (browser ki zaroorat nahi)
    seo_leads: list[dict] = []
    for lead in leads:
        key = lead.get("feature_id") or lead.get("gmb_url")
        gmb = audit_gmb(lead)
        record = audits.get(key, {})
        record.update({"lead_key": key, "name": lead.get("name", ""), **gmb})
        audits[key] = record
        if gmb["track"] == TRACK_SEO and "site" not in record:
            seo_leads.append(lead)

    no_site = sum(1 for a in audits.values() if a["track"] != TRACK_SEO)
    print(
        paint("  ✓ GMB audit done", GREEN)
        + paint("   website nahi: " + str(no_site) + " · website hai: " + str(len(leads) - no_site), DIM)
    )

    if not seo_leads:
        print(paint("  Koi site audit karne ko nahi bacha.", DIM))
    else:
        print(paint("  → " + str(len(seo_leads)) + " websites deep audit ho rahi hain…", CYAN))
        started = time.time()
        done = {"n": 0}

        def on_done(index: int, lead: dict, audit: dict) -> None:
            done["n"] += 1
            score = audit.get("score", 0)
            status = "DOWN" if not audit.get("reachable") else str(score) + "/100"
            sys.stdout.write(
                "\r\033[K  ["
                + str(done["n"])
                + "/"
                + str(len(seo_leads))
                + "] "
                + paint(status.ljust(8), score_colour(score))
                + lead.get("name", "")[:44]
            )
            sys.stdout.flush()

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                results = await audit_many(
                    browser, seo_leads, concurrency=args.concurrency, on_done=on_done
                )
            finally:
                await browser.close()

        for lead, audit in zip(seo_leads, results):
            key = lead.get("feature_id") or lead.get("gmb_url")
            audits[key]["site"] = audit
            audits[key]["score"] = audit.get("score", 0)

        print("\r\033[K  " + paint("✓ Site audits done", GREEN) + paint(
            "  (" + str(int(time.time() - started)) + "s)", DIM))

    # Website hai hi nahi = digital presence score 0 (site quality ka sawal hi nahi).
    # GMB completeness ka apna score alag `gmb_score` me rehta hai.
    for record in audits.values():
        if "site" not in record:
            record["score"] = 0

    out_path.write_text(json.dumps(audits, indent=1, ensure_ascii=False), encoding="utf-8")

    scored = sorted(audits.values(), key=lambda a: a.get("score", 100))
    print()
    print(paint("─" * 66, DIM))
    print(paint(BOLD + "  Sabse zyada pain wale 10 leads (kam score = better lead)" + RESET, CYAN))
    for record in scored[:10]:
        score = record.get("score", 0)
        track = record.get("track", "")
        top = ""
        pool = record.get("site", {}).get("findings") or record.get("gmb_findings") or []
        if pool:
            top = pool[0]["title"]
        print(
            "  "
            + paint(str(score).rjust(3), score_colour(score))
            + "  "
            + track.ljust(8)
            + record.get("name", "")[:32].ljust(34)
            + paint(top[:40], DIM)
        )
    print(paint("─" * 66, DIM))
    print("  " + paint("File: ", DIM) + str(out_path))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m leadengine.audit")
    parser.add_argument("target", help="search-id ya NDJSON path")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--fresh", action="store_true", help="purane audits ignore karo")
    args = parser.parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
