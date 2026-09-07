"""CLI: har lead ke liye Email + Instagram DM + SMS likhwao.

    python -m leadengine.messages car-wash-new-york
    python -m leadengine.messages car-wash-new-york --language roman_urdu --top 20
    python -m leadengine.messages car-wash-new-york --templates-only
    python -m leadengine.messages car-wash-new-york --keys 0x89c2f6011b0f7f55:0x29ad69900ce83474

Input : <search-id>.ndjson + .audit.json + .scores.json
Output: <search-id>.messages.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from leadengine.messages import claude_cli, templates
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


def load(sid: str) -> tuple[dict, dict, dict]:
    leads_path = DATA_DIR / (sid + ".ndjson")
    audits_path = DATA_DIR / (sid + ".audit.json")
    scores_path = DATA_DIR / (sid + ".scores.json")
    for path, hint in (
        (leads_path, "python -m leadengine.scrape ..."),
        (audits_path, "python -m leadengine.audit " + sid),
        (scores_path, "python -m leadengine.scoring " + sid),
    ):
        if not path.exists():
            raise SystemExit("Missing: " + path.name + "\nPehle ye chalao:  " + hint)

    leads = {}
    for line in leads_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            leads[row.get("feature_id") or row.get("gmb_url")] = row
    audits = json.loads(audits_path.read_text(encoding="utf-8"))
    scores = json.loads(scores_path.read_text(encoding="utf-8"))["leads"]
    return leads, audits, scores


def run(args) -> int:
    sid = Path(args.target).stem.replace(".scores", "")
    leads, audits, scores = load(sid)

    only = {k.strip() for k in (args.keys or "").split(",") if k.strip()}

    ranked = sorted(scores.values(), key=lambda r: -r["lead_score"])
    if only:
        ranked = [r for r in ranked if r["lead_key"] in only]
        missing = only - {r["lead_key"] for r in ranked}
        if missing:
            raise SystemExit("Ye keys scores me nahi mile: " + ", ".join(sorted(missing)))
    elif args.top:
        ranked = ranked[: args.top]

    out_path = DATA_DIR / (sid + ".messages.json")
    existing = {}
    if out_path.exists() and (only or not args.fresh):
        existing = json.loads(out_path.read_text(encoding="utf-8"))

    todo = [r for r in ranked if only or args.fresh or r["lead_key"] not in existing]

    print(paint(BOLD + "\n  Messages: " + sid + RESET, CYAN))
    print(
        paint(
            "  " + str(len(todo)) + " leads · language " + args.language
            + (" · templates only" if args.templates_only else " · Claude CLI"),
            DIM,
        )
    )
    if existing and not args.fresh:
        print(paint("  ↻ " + str(len(existing)) + " pehle se likhe ja chuke hain", YELLOW))
    print()

    results: dict[str, dict] = dict(existing)
    stats = {"ai": 0, "template": 0, "cost": 0.0}

    def fallback(record: dict, reason: str) -> None:
        key = record["lead_key"]
        results[key] = {
            "lead_key": key,
            "name": record["name"],
            "language": args.language,
            "generated_by": "template",
            "reason": reason,
            **templates.build(leads[key], audits.get(key, {}), record, args.language),
        }
        stats["template"] += 1

    if args.templates_only or not claude_cli.available():
        if not args.templates_only:
            print(paint("  ! claude CLI nahi mili — templates use ho rahe hain", YELLOW))
        for record in todo:
            fallback(record, "templates-only")
    else:
        batches = list(claude_cli.chunks(todo, args.batch_size))
        done = {"n": 0}
        started = time.time()

        def process(batch: list[dict]) -> None:
            briefs = [
                claude_cli.build_brief(
                    leads[r["lead_key"]], audits.get(r["lead_key"], {}), r, args.language
                )
                for r in batch
            ]
            try:
                result = claude_cli.generate_batch(briefs, model=args.model)
                stats["cost"] += result.cost_usd or 0
                for record in batch:
                    key = record["lead_key"]
                    payload = result.messages.get(key)
                    if payload and payload.get("email"):
                        results[key] = {
                            "lead_key": key,
                            "name": record["name"],
                            "language": args.language,
                            "generated_by": "claude_cli",
                            **payload,
                        }
                        stats["ai"] += 1
                    else:
                        fallback(record, "model ne is lead ka jawab nahi diya")
            except Exception as exc:  # noqa: BLE001 - fallback hamesha mojood hai
                for record in batch:
                    fallback(record, str(exc)[:120])
            done["n"] += len(batch)
            sys.stdout.write(
                "\r\033[K  ["
                + str(done["n"])
                + "/"
                + str(len(todo))
                + "]  AI "
                + str(stats["ai"])
                + " · template "
                + str(stats["template"])
            )
            sys.stdout.flush()

        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            list(pool.map(process, batches))

        print(
            "\r\033[K  "
            + paint("✓ Done", GREEN)
            + paint("  (" + str(int(time.time() - started)) + "s)", DIM)
        )

    out_path.write_text(json.dumps(results, indent=1, ensure_ascii=False), encoding="utf-8")

    print()
    print(paint("  AI se likhe: ", DIM) + str(stats["ai"]) + paint("   template se: ", DIM) + str(stats["template"]))
    if stats["cost"]:
        print(paint("  Subscription usage (approx $ equivalent): ", DIM) + "$" + str(round(stats["cost"], 2)))

    sample_key = ranked[0]["lead_key"] if ranked else None
    sample = results.get(sample_key)
    if sample:
        print()
        print(paint("─" * 70, DIM))
        print(paint(BOLD + "  Sample — " + sample["name"] + RESET, CYAN)
              + paint("  (" + sample["generated_by"] + ")", DIM))
        print()
        email = sample["email"][0]
        print(paint("  EMAIL", YELLOW) + "  subject: " + email["subject"])
        for line in email["body"].splitlines():
            print("    " + line)
        print()
        print(paint("  INSTAGRAM DM", YELLOW))
        print("    " + sample["instagram"][0])
        print()
        print(paint("  SMS", YELLOW))
        print("    " + sample["sms"][0])
        print(paint("─" * 70, DIM))

    print("  " + paint("File: ", DIM) + str(out_path))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m leadengine.messages")
    parser.add_argument("target", help="search-id")
    parser.add_argument(
        "--language", default="en", choices=list(claude_cli.LANGUAGES), help="copy ki zubaan"
    )
    parser.add_argument("--top", type=int, default=None, help="sirf top N leads")
    parser.add_argument(
        "--keys",
        default=None,
        help="sirf in lead keys ke messages (comma se alag). Ye hamesha dobara likhe jate hain.",
    )
    parser.add_argument("--batch-size", type=int, default=claude_cli.BATCH_SIZE)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--model", default=claude_cli.DEFAULT_MODEL)
    parser.add_argument("--templates-only", action="store_true")
    parser.add_argument("--fresh", action="store_true")
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
