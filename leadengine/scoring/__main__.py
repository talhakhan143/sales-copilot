"""CLI: audited leads ko score karo aur paisa lagao.

    python -m leadengine.scoring car-wash-new-york

Input : <search-id>.ndjson  +  <search-id>.audit.json
Output: <search-id>.scores.json   (feature_id -> score/money/pain)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from leadengine.scoring.money import expected_value, price_lead
from leadengine.scoring.pain import build_pain_points, build_talking_points
from leadengine.scoring.score import market_context, score_lead
from leadengine.settings import DATA_DIR

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

RESET, DIM, BOLD = "\033[0m", "\033[2m", "\033[1m"
GREEN, YELLOW, CYAN, MAGENTA = "\033[32m", "\033[33m", "\033[36m", "\033[35m"
RED = "\033[31m"


def paint(text: str, code: str) -> str:
    return code + text + RESET if sys.stdout.isatty() else text


def money_str(symbol: str, amount: int) -> str:
    if amount >= 1000:
        return symbol + str(round(amount / 1000, 1)).rstrip("0").rstrip(".") + "k"
    return symbol + str(amount)


def run(args) -> int:
    sid = Path(args.target).stem.replace(".audit", "")
    leads_path = DATA_DIR / (sid + ".ndjson")
    audits_path = DATA_DIR / (sid + ".audit.json")
    if not leads_path.exists():
        raise SystemExit("Leads file nahi mili: " + str(leads_path))
    if not audits_path.exists():
        raise SystemExit(
            "Audit file nahi mili. Pehle ye chalao:  python -m leadengine.audit " + sid
        )

    leads = [
        json.loads(line)
        for line in leads_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    audits = json.loads(audits_path.read_text(encoding="utf-8"))
    context = market_context(leads, audits)

    out: dict[str, dict] = {}
    for lead in leads:
        key = lead.get("feature_id") or lead.get("gmb_url")
        audit = audits.get(key, {"track": "WEBSITE", "score": 0, "gmb_findings": []})
        money = price_lead(lead, audit)
        score = score_lead(lead, audit, money, context)
        money["close_probability"] = round(
            expected_value(money, score["urgency"]) / max(1, money["contract_value_usd"]), 3
        )
        money["expected_value_usd"] = expected_value(money, score["urgency"])
        out[key] = {
            "lead_key": key,
            "name": lead.get("name", ""),
            "track": audit.get("track"),
            "subtrack": audit.get("subtrack"),
            "presence_score": audit.get("score", 0),
            **score,
            "money": money,
            "pain_points": build_pain_points(lead, audit, money, context),
            "talking_points": build_talking_points(lead, audit, money, score, context),
        }

    out_path = DATA_DIR / (sid + ".scores.json")
    out_path.write_text(
        json.dumps({"context": context, "leads": out}, indent=1, ensure_ascii=False),
        encoding="utf-8",
    )

    symbol = next(iter(out.values()))["money"]["currency_symbol"] if out else "$"
    pipeline = sum(r["money"]["contract_value_usd"] for r in out.values())
    weighted = sum(r["money"]["expected_value_usd"] for r in out.values())

    print()
    print(paint(BOLD + "  " + sid + RESET, CYAN))
    print(
        paint("  " + str(len(out)) + " leads · ", DIM)
        + paint("pipeline " + money_str(symbol, pipeline), MAGENTA)
        + paint("  ·  realistic " + money_str(symbol, weighted), DIM)
    )

    # Dono track alag dikhate hain - inke need scores aapas me compare
    # karne wali cheez nahi hain.
    for track, heading in (
        ("WEBSITE", "WEBSITE bechni hai"),
        ("SEO", "SEO bechni hai"),
    ):
        rows = sorted(
            [r for r in out.values() if r["track"] == track],
            key=lambda r: -r["need_score"],
        )
        if not rows:
            continue
        value = sum(r["money"]["contract_value_usd"] for r in rows)
        print()
        print(
            paint(BOLD + "  " + heading + RESET, CYAN)
            + paint("   " + str(len(rows)) + " leads · " + money_str(symbol, value), DIM)
        )
        print(paint("  " + "─" * 76, DIM))
        for record in rows[:12]:
            need = record["need_score"]
            colour = RED if need >= 75 else (YELLOW if need >= 55 else DIM)
            print(
                "  "
                + paint(str(need).rjust(3), colour)
                + paint(" need", DIM)
                + "  "
                + money_str(symbol, record["money"]["contract_value_usd"]).ljust(8)
                + record["name"][:28].ljust(30)
                + paint(record["need_reason"][:44], DIM)
            )
        if len(rows) > 12:
            print(paint("  … and " + str(len(rows) - 12) + " more leads", DIM))

    buckets = {}
    for record in out.values():
        buckets[record["priority"]] = buckets.get(record["priority"], 0) + 1
    print()
    print(
        paint("  Do today: ", DIM) + str(buckets.get("now", 0))
        + paint("   ·  This week: ", DIM) + str(buckets.get("week", 0))
        + paint("   ·  Later: ", DIM) + str(buckets.get("later", 0))
    )
    print("  " + paint("File: ", DIM) + str(out_path))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m leadengine.scoring")
    parser.add_argument("target", help="search-id")
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
