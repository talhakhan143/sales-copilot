"""Claude CLI ko headless chala kar outreach copy likhwana.

`claude -p --output-format json` — tumhari mojooda subscription use hoti hai,
koi API key ya extra kharcha nahi.

Ek call me kai leads bhejte hain (batching) kyunke har CLI call ke saath
~45k tokens ka system/tools overhead aata hai. 4 leads per call se wo
overhead 4 leads pe bant jata hai.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import dataclass

from leadengine.settings import PROMPTS_DIR, load_config

CLAUDE_BIN = shutil.which("claude") or "claude"
DEFAULT_MODEL = "claude-sonnet-5"
BATCH_SIZE = 4
CALL_TIMEOUT = 240

SYSTEM_PROMPT = (PROMPTS_DIR / "copywriter.md").read_text(encoding="utf-8")


def sender_profile() -> dict:
    """Tumhari details - har message me yehi jayengi (model naam invent na kare)."""
    raw = load_config("profile.json")
    return {k: v for k, v in raw.items() if v and not k.startswith("_")}

LANGUAGES = ("en", "roman_urdu", "urdu")


class ClaudeCLIError(RuntimeError):
    pass


def available() -> bool:
    return bool(shutil.which("claude"))


# ------------------------------------------------------------------- openers

# Sab se bara masla: har lead ka message ek jaisa lagta tha, kyunke model har
# dafa wohi shakl pakar leta hai (stat dump -> "but" -> offer). Har brief ko ek
# opener assign karte hain, aur ek batch me do lead ko same opener nahi dete —
# model ke paas groove me girne ka rasta hi na bache.
OPENERS = (
    "competitor",
    "customer_moment",
    "single_detail",
    "their_strength",
    "direct_offer",
    "question",
)


def _opener_allowed(opener: str, brief: dict) -> bool:
    """Kuch angle sirf tab chalte hain jab brief me un ke liye maal mojood ho."""
    if opener == "competitor":
        return bool(brief.get("rank_in_local_search"))
    if opener == "their_strength":
        return bool(brief.get("google_reviews") or brief.get("google_rating"))
    if opener == "single_detail":
        return bool(brief.get("problems"))
    return True


def pick_opener(brief: dict) -> str:
    """Stable choice — ek hi lead dobara generate karo to wohi angle aaye."""
    pool = [o for o in OPENERS if _opener_allowed(o, brief)] or ["direct_offer"]
    digest = hashlib.sha1(str(brief.get("id", "")).encode("utf-8")).hexdigest()
    return pool[int(digest[:8], 16) % len(pool)]


def spread_openers(briefs: list[dict]) -> None:
    """Ek batch ke andar duplicate openers ko alag kar do (in place)."""
    used: set[str] = set()
    for brief in briefs:
        current = brief.get("open_with") or pick_opener(brief)
        if current in used:
            for alt in OPENERS:
                if alt not in used and _opener_allowed(alt, brief):
                    current = alt
                    break
        brief["open_with"] = current
        used.add(current)


# --------------------------------------------------------------------- brief

def build_brief(lead: dict, audit: dict, scored: dict, language: str = "en") -> dict:
    """Chhota, saaf brief. Jitna kam shor, utni behtar copy."""
    money = scored["money"]
    site = audit.get("site") or {}
    metrics = site.get("metrics", {})

    brief = {
        "id": scored["lead_key"],
        "language": language,
        "sender": sender_profile(),
        "business": lead.get("name", ""),
        "city": lead.get("city") or lead.get("search_location", ""),
        "industry": lead.get("category") or lead.get("search_niche", ""),
        "deal_type": money["deal_type"],
        "track": money["deal_type"],
        "sender_offers": (
            "a finished website built for them, free to look at - they pay only if they like it"
            if money["deal_type"] == "WEBSITE"
            else "a free written breakdown of what is losing them customers, no obligation"
        ),
    }

    if lead.get("review_count"):
        brief["google_reviews"] = lead["review_count"]
    if lead.get("rating"):
        brief["google_rating"] = lead["rating"]
    if scored.get("local_rank"):
        brief["rank_in_local_search"] = scored["local_rank"]
    if audit.get("track") == "SEO":
        brief["website"] = site.get("final_url") or lead.get("website")
        brief["website_score_out_of_100"] = audit.get("score")
        if metrics.get("load_ms"):
            brief["homepage_load_seconds"] = round(metrics["load_ms"] / 1000, 1)
        if metrics.get("word_count") is not None:
            brief["homepage_word_count"] = metrics["word_count"]
        if site.get("platform"):
            brief["built_with"] = site["platform"]
    if audit.get("subtrack") == "SOCIAL_ONLY":
        brief["only_has_social_page"] = lead.get("website")

    # Pain points hi asal maal hain - inhi se copy banti hai.
    brief["problems"] = [
        {"headline": p["title"], "explanation": p["detail"], "proof": p.get("proof", "")}
        for p in scored.get("pain_points", [])[:3]
    ]

    # Opener aakhir me — kyunke ye baqi fields dekh kar chunta hai.
    brief["open_with"] = pick_opener(brief)
    return brief


# ------------------------------------------------------------------- calling

def _extract_json(raw: str) -> dict:
    """Model kabhi kabhi fence ya thori prose laga deta hai — bahar nikaal lo."""
    text = (raw or "").strip()
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start : end + 1])
    raise ClaudeCLIError("JSON parse nahi hua: " + text[:200])


def _run_claude(prompt: str, model: str) -> dict:
    cmd = [
        CLAUDE_BIN,
        "-p",
        "--output-format",
        "json",
        "--model",
        model,
        "--no-session-persistence",
        "--append-system-prompt",
        SYSTEM_PROMPT,
    ]
    proc = subprocess.run(
        cmd,
        input=prompt,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=CALL_TIMEOUT,
    )
    if proc.returncode != 0:
        raise ClaudeCLIError(
            "claude CLI exit " + str(proc.returncode) + ": " + (proc.stderr or "")[:300]
        )
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ClaudeCLIError("CLI ne JSON nahi diya: " + proc.stdout[:200]) from exc
    if envelope.get("is_error"):
        raise ClaudeCLIError("CLI error: " + str(envelope.get("result"))[:200])
    return {
        "payload": _extract_json(envelope.get("result", "")),
        "usage": envelope.get("usage", {}),
        "cost_usd": envelope.get("total_cost_usd", 0),
    }


@dataclass
class BatchResult:
    messages: dict          # lead_key -> {email, instagram, sms}
    cost_usd: float = 0.0
    output_tokens: int = 0
    failed: list[str] = None  # type: ignore[assignment]


def generate_batch(briefs: list[dict], model: str = DEFAULT_MODEL, retries: int = 1) -> BatchResult:
    """Ek batch ke liye copy banwao. Parse fail ho to ek dafa retry."""
    if not briefs:
        return BatchResult(messages={}, failed=[])

    spread_openers(briefs)

    prompt = (
        "Write outreach copy for these "
        + str(len(briefs))
        + " leads. Return one JSON object keyed by each lead's id, "
        "with exactly 2 variants per channel. JSON only.\n\n"
        "Each brief carries an `open_with` field — honour it. When the drafts are "
        "done, reread them as a set: if any two could be swapped by find-and-"
        "replacing the business name, rewrite one before you answer.\n\n"
        + json.dumps(briefs, ensure_ascii=False, indent=1)
    )

    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            out = _run_claude(prompt, model)
            payload = out["payload"]
            missing = [b["id"] for b in briefs if b["id"] not in payload]
            return BatchResult(
                messages=payload,
                cost_usd=out.get("cost_usd", 0) or 0,
                output_tokens=out.get("usage", {}).get("output_tokens", 0),
                failed=missing,
            )
        except Exception as exc:  # noqa: BLE001 - retry ke baad caller fallback karega
            last_error = exc
            prompt += "\n\nIMPORTANT: your previous reply was not valid JSON. Return ONLY the JSON object."
    raise ClaudeCLIError(str(last_error))


def chunks(items: list, size: int = BATCH_SIZE):
    for start in range(0, len(items), size):
        yield items[start : start + size]
