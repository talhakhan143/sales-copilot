"""Prompt templates and the frozen quick action catalogue.

Everything the LLM ever sees, other than live transcript turns, is assembled
here. The module is pure text, it imports nothing from the rest of the app and
performs no IO, so it can be imported from any layer and unit tested on its own.

Two entry points matter:
    ``build_system_prompt`` fuses the knowledge base, the scraped prospect block,
    the call goal and the answer language into the single system message.
    ``quick_action_prompt`` turns one of the eight frozen objection buttons into
    a synthetic user message that reads like the prospect just said it.
"""

from __future__ import annotations

SYSTEM_TEMPLATE: str = """# ROLE
You are an elite real time cold call copilot sitting beside a sales representative who is
a non native English speaker. The rep reads your output ALOUD, word for word, while the
prospect is on the line. Everything you write is spoken within two seconds.

# MY SERVICES & OFFERS
{knowledge_base}

# TARGET CLIENT INFO
{client_block}

# CALL GOAL
{call_goal}

# TASK
Read what the prospect just said and reply with the single best thing for the rep to say
next: a natural conversational response, or a fix for the objection they just raised.

# OUTPUT RULES
1. Keep it very short. Two to four short sentences, and never more than 35 words in total.
2. Use the simplest English you can. Aim at a grade 4 reading level, the kind of English
   a nine year old can read out loud on sight and a stranger will still take seriously.
   Prefer one and two syllable words. "use" not "utilise", "help" not "facilitate",
   "cut" not "reduce", "show" not "demonstrate", "buy" not "procure".
3. Each sentence must be short enough to say in one breath. Aim for eight to twelve words
   per sentence. Break a long thought into two short sentences instead of using a comma.
4. Banned: idioms, slang, sales jargon, buzzwords, and any phrase a non native speaker
   would have to think about. No "circle back", "touch base", "value proposition",
   "leverage", "synergy", "reach out", "at the end of the day", "moving forward".
5. Write numbers the way they are spoken, so "fifteen minutes" not "15 min", and
   "two thousand five hundred dollars" not "$2,500".
6. Speak as the rep, first person. Never describe what to do, just write the words.
7. No preamble, no quotes, no labels, no markdown, no bullet points, no emoji.
8. Ground every claim in MY SERVICES & OFFERS. If you do not know, say you will confirm.
9. End on a short, easy question that moves the call forward.
10. Never use an em dash. Use a comma or a period.
11. If the prospect said something small (a greeting, filler, unclear audio), reply with
    the shortest natural human answer and nothing else.
12. Answer in {language}."""


# ===========================================================================
# Reading style
# ===========================================================================

#: The two ways the teleprompter can feed a rep.
STYLES: Final[tuple[str, ...]] = ("full", "points")

DEFAULT_STYLE: Final[str] = "full"

POINTS_DIRECTIVE: str = """# OVERRIDE, POINTS MODE

Forget rule 1 of the OUTPUT RULES above. The rep is not reading you out loud. They
glance at you and then say it themselves, in their own words, so it sounds like a
person and not like somebody reading.

So give them the PIECES OF WHAT THEY WOULD SAY, in the order they would say them.
They glue the pieces together with their own small words.

1. Four pieces. Five at the very most, and only when the fifth earns its place.
   This is a glance, not a list. Six is already too many to take in while
   somebody is waiting on the phone.
2. Each piece is two to six words, and it must be words that come out of a mouth.
   Say the thing. Do not name the thing.
3. Put them in speaking order, so reading top to bottom builds the answer.
4. Never write an instruction to the rep. "ask for the meeting" is a note to
   themselves. "15 minutes to show you" is what they say. Always write the second.
5. Never write a label for an argument. "price is fair" is a label, nobody says
   it. "less than one lost order" is the argument, in speakable words.
6. Keep the real numbers from MY SERVICES & OFFERS, as digits, so "3500 USD" and
   "61 percent" and "2 to 4 weeks". A number is the one thing the rep cannot make
   up while talking, and digits are quicker to catch than words.
   NEVER flip what a number means to make it sound better. Squeezing a proof into
   four words is exactly where that happens. If the proof says calls went DOWN by
   61 percent, "61 percent more calls" is a lie the rep will say out loud to a
   real person. Copy the direction as carefully as the digits.
7. One piece per line. No dash, no bullet, no number, no full stop.
8. The last piece is the ask, written as the words they would say.
9. Still plain grade 4 English, and still no em dash.

The test: could the rep say each line out loud, more or less as written, and would
stringing them together sound like a person talking? If a line fails that, it is
the wrong line.

The client said their price is too high. Good:

I hear you
less than one lost order
Aster cut calls 61 percent
3500 USD, ready in 2 weeks
15 minutes to show you

Reading that, a rep naturally says: "I hear you. Honestly it is less than one lost
order costs you. We did this for Aster Dental and their calls dropped 61 percent.
It is 3500 dollars and ready in two weeks. Can I get 15 minutes to show you?"

Bad, because these are labels and notes, not words anybody says:

price is fair
mention the proof
ask for 15 minutes
handle the objection

Bad, because these are whole sentences to read:

I understand that the price feels high to you.
Can we book a fifteen minute demo this week?
"""
"""Appended as the last system message when the rep wants points.

It goes last on purpose. The base prompt tells the model to write a line to read
out loud, and the freshest instruction is the one a model follows, so this has to
sit after it rather than being spliced into it. Keeping the two apart also means
the style can be flipped in the middle of a live call without rebuilding the
session's system prompt.
"""


def style_directive(style: str) -> str | None:
    """Return the extra system message for a reading style.

    Args:
        style: ``"full"`` or ``"points"``. Anything unknown is treated as
            ``"full"``, because a typo should not silently change how the
            teleprompter reads mid call.

    Returns:
        The directive to append, or None when the base prompt already says it.
    """
    return POINTS_DIRECTIVE if str(style).strip().lower() == "points" else None

NO_CLIENT_INFO: str = (
    "No public information was fetched for this prospect. Ask discovery questions "
    "before making claims."
)
"""Filler for the TARGET CLIENT INFO block when the scrape failed or no URL was given."""

NO_KNOWLEDGE_BASE: str = (
    "The rep did not provide an offer sheet. Never invent a product detail, a price or a "
    "case study. Ask questions and promise to confirm specifics after the call."
)
"""Filler for MY SERVICES & OFFERS when the knowledge base somehow arrives empty."""

DEFAULT_CALL_GOAL: str = (
    "Qualify this prospect, find the one problem we can actually solve for them, and book "
    "a short follow up call."
)
"""Used when the rep leaves the call goal field blank."""

LANGUAGE_NAMES: dict[str, str] = {
    "en": "English",
    "ur": "Urdu",
    "hi": "Hindi",
    "es": "Spanish",
    "ar": "Arabic",
    "fr": "French",
    "de": "German",
}
"""Two letter code to the human readable name that goes into the prompt."""

DEFAULT_LANGUAGE_NAME: str = "English"
"""Fallback language name for an unknown code."""

QUICK_ACTIONS: list[dict[str, str]] = [
    {
        "key": "too_expensive",
        "label": "Too Expensive",
        "icon": "BadgeDollarSign",
        "hint": "Price objection",
    },
    {
        "key": "not_interested",
        "label": "Not Interested",
        "icon": "Ban",
        "hint": "Brush off",
    },
    {
        "key": "send_email",
        "label": "Send Me An Email",
        "icon": "Mail",
        "hint": "Deflection",
    },
    {
        "key": "have_vendor",
        "label": "Already Have Vendor",
        "icon": "Building2",
        "hint": "Incumbent",
    },
    {
        "key": "no_time",
        "label": "No Time Right Now",
        "icon": "Clock",
        "hint": "Time objection",
    },
    {
        "key": "who_are_you",
        "label": "Who Are You?",
        "icon": "HelpCircle",
        "hint": "Cold open",
    },
    {
        "key": "send_proposal",
        "label": "Send A Proposal",
        "icon": "FileText",
        "hint": "Positive signal",
    },
    {
        "key": "book_meeting",
        "label": "Book The Meeting",
        "icon": "CalendarCheck",
        "hint": "Close",
    },
]
"""The eight frozen objection buttons, in display order.

Icon names are lucide-react components that exist in the library. The contract
listed "HandX" for the brush off action, that component does not exist in
lucide-react, so the approved substitute "Ban" is used instead.
"""

QUICK_ACTION_MAP: dict[str, dict[str, str]] = {action["key"]: action for action in QUICK_ACTIONS}
"""Lookup from quick action key to its full entry."""

_QUICK_ACTION_INSTRUCTIONS: dict[str, str] = {
    "too_expensive": (
        "The prospect just pushed back on price. They said some version of this costs too "
        "much or is not in the budget. Give me the words that hold my price without "
        "apologising, put the number next to what their current situation is already "
        "costing them every month, and end by asking what they are spending on the problem "
        "today. Do not discount, do not get defensive, do not explain my pricing model."
    ),
    "not_interested": (
        "The prospect brushed me off with a flat not interested, and they said it before "
        "they heard anything real. Give me the words that buy ten more seconds: agree "
        "lightly so they relax, name in one line the exact problem people in their seat "
        "usually have, then ask a soft yes or no question. Stay warm, never argue with the "
        "brush off, and never sound like I am begging."
    ),
    "send_email": (
        "The prospect asked me to just send an email or send some information, which is a "
        "polite way to end the call. Give me the words that agree to send it and, in the "
        "same breath, ask the one question I need answered so the email is actually about "
        "them. Aim to trade the email for a short slot on the calendar."
    ),
    "have_vendor": (
        "The prospect says they already work with somebody for this. Give me the words that "
        "respect the current vendor, never criticise them, and open a small gap by asking "
        "about the one thing that kind of vendor usually does not cover. Position me as the "
        "backup they call when something slips, not as a replacement they must justify today."
    ),
    "no_time": (
        "The prospect says they have no time right now or they are walking into something. "
        "Give me the words that accept it in three or four words, compress my whole reason "
        "for calling into one short line, and then ask for a specific short slot later. "
        "Offer two exact options so answering is one word for them."
    ),
    "who_are_you": (
        "The prospect wants to know who I am and why I am calling, so my opener did not "
        "land. Give me the words for a clean one breath introduction: my name, what we do "
        "for people like them in plain language, and the honest reason I picked their "
        "company. Hand the call straight back to them with a question at the end."
    ),
    "send_proposal": (
        "The prospect asked for a proposal, so this is a buying signal and not an "
        "objection. Give me the words that say yes with energy and immediately pin down "
        "what the proposal must cover: the scope they care about, a budget range, and who "
        "else reads it. Push to walk them through it live instead of sending it cold."
    ),
    "book_meeting": (
        "The prospect is warm and it is time to close for the meeting. Give me the words "
        "that assume the meeting is happening, say in one short line what we will cover and "
        "how long it takes, and offer two exact time slots. Finish by asking them to "
        "confirm the best email, then stop."
    ),
}

_QUICK_ACTION_TAIL: str = (
    "Reply with only the words I should say out loud right now, following every rule in "
    "the OUTPUT RULES section."
)


def quick_action_prompt(key: str, note: str = "") -> str:
    """Build the synthetic user message for one quick action button.

    The message is written from the rep's point of view and states exactly which
    objection the prospect just raised, so the model answers with a rebuttal
    instead of asking what happened. The output rules from the system prompt are
    restated at the end because a quick action can fire long after the system
    message, deep in a transcript window.

    Args:
        key: One of the frozen keys in ``QUICK_ACTION_MAP``.
        note: Optional free text the rep typed alongside the button, for example
            the actual number the prospect quoted. Ignored when blank.

    Returns:
        A single user message string ready to append to the LLM message list.

    Raises:
        KeyError: If ``key`` is not one of the eight frozen quick action keys.
    """
    if key not in _QUICK_ACTION_INSTRUCTIONS:
        raise KeyError(f"Unknown quick action key: {key!r}")

    action = QUICK_ACTION_MAP[key]
    parts = [
        f"QUICK ACTION: {action['label']} ({action['hint']}).",
        _QUICK_ACTION_INSTRUCTIONS[key],
        _QUICK_ACTION_TAIL,
    ]
    cleaned_note = note.strip()
    if cleaned_note:
        parts.append(f"Extra context from the rep: {cleaned_note}")
    return "\n\n".join(parts)


def language_name(code: str) -> str:
    """Expand a two letter language code into a human readable name.

    Args:
        code: A language code such as ``"ur"``. Case and surrounding whitespace
            do not matter.

    Returns:
        The language name, for example ``"Urdu"``. Unknown codes return
        ``"English"`` so the prompt is never left with a bare code in it.
    """
    return LANGUAGE_NAMES.get(code.strip().lower(), DEFAULT_LANGUAGE_NAME)


def build_system_prompt(
    *,
    knowledge_base: str,
    client_block: str,
    call_goal: str,
    language: str,
) -> str:
    """Fuse every static piece of context into the single system message.

    Each block gets a safe fallback so the prompt never contains an empty
    heading, which reads to the model as missing information and invites it to
    invent details.

    Args:
        knowledge_base: The rep's offer sheet, already clamped to the configured
            character budget by the caller.
        client_block: The scraped prospect text with its title and URL on top,
            or an empty string when nothing was fetched.
        call_goal: What this call must achieve. Blank falls back to a sensible
            qualify and book goal.
        language: Two letter code the copilot must answer in. It is expanded to
            a human name inside the prompt.

    Returns:
        The complete system prompt string.
    """
    kb = knowledge_base.strip() or NO_KNOWLEDGE_BASE
    block = client_block.strip() or NO_CLIENT_INFO
    goal = call_goal.strip() or DEFAULT_CALL_GOAL
    return SYSTEM_TEMPLATE.format(
        knowledge_base=kb,
        client_block=block,
        call_goal=goal,
        language=language_name(language),
    )


__all__ = [
    "SYSTEM_TEMPLATE",
    "NO_CLIENT_INFO",
    "NO_KNOWLEDGE_BASE",
    "DEFAULT_CALL_GOAL",
    "LANGUAGE_NAMES",
    "DEFAULT_LANGUAGE_NAME",
    "QUICK_ACTIONS",
    "QUICK_ACTION_MAP",
    "quick_action_prompt",
    "language_name",
    "build_system_prompt",
]
