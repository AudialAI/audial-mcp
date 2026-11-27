"""What the server may say to ChatGPT about an account's runs: plainly how many are left.

ChatGPT's plugin rules: a plugin "must not display subscription plans, initiate new
subscriptions, or promote upgrades", and must not link to a checkout or transactional page
(plugins/plugin-guidelines). Audial's own refusals do all of that ("Subscribe at
audialmusic.ai", "More calls: <pricing page>"), so when the caller is ChatGPT
(`remote.is_chatgpt`) refusals and status messages are reworded here. Claude and local users
keep the usual wording.

Only Audial's own words are changed: a message, an error, a notice. What a tool returns about
the user's audio (lyrics, tags, descriptions) is the user's content and is never touched.
"""

from __future__ import annotations

import re
from typing import Any

from audial.api.exceptions import AudialLimitReached

NO_RUNS_LEFT = (
    "The Audial account connected here has no runs left, so Audial did not run this. "
    "Retrying will not help."
)
# What is left when every sentence of a message was about buying something.
NOTHING_TO_SAY = "Audial could not run this for the connected account."

# The API's refusal when the free runs are used up (HTTP 402), as its code and as its words.
_NO_RUNS = re.compile(
    r"SUBSCRIPTION_REQUIRED|needs an active Audial subscription|subscribe at", re.I
)
# A link to a page that sells something ("More calls: https://audialmusic.ai/product/pricing").
_SALES_LINK = re.compile(
    r"https?://\S*(?:pricing|checkout|billing|upgrade|subscri|plans|stripe\.com)", re.I
)
_SALES_WORD = re.compile(
    r"\b(?:subscri\w*|upgrad\w*|pricing|prices?|checkout|top[- ]?ups?|trials?|purchas\w*|"
    r"buy\w*|discount\w*|promo\w*)\b",
    re.I,
)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")

# Keys whose string values are Audial's words rather than the user's content.
_MESSAGE_KEYS = frozenset({"error", "message", "notice", "warning", "warnings", "detail", "reason"})


def no_runs_this_month(exc: AudialLimitReached) -> str:
    """The monthly limit, as a count and a date: no plan named, no link to more."""
    counted = (
        f" ({exc.used} of {exc.limit})" if exc.used is not None and exc.limit is not None else ""
    )
    text = (
        "The Audial account connected here has used all of this month's runs"
        f"{counted}, so Audial did not run this."
    )
    if exc.resets_at is not None:
        day = exc.resets_at.strftime("%Y-%m-%d")
        return f"{text} They reset on {day}; retrying before then will not help."
    return f"{text} Retrying now will not help."


def plain_text(text: str) -> str:
    """One of Audial's messages without its sentences that sell or link to a sale."""
    if _NO_RUNS.search(text):
        return NO_RUNS_LEFT
    kept = [
        sentence
        for sentence in _SENTENCE_END.split(text.strip())
        if sentence and not (_SALES_WORD.search(sentence) or _SALES_LINK.search(sentence))
    ]
    return " ".join(kept) or NOTHING_TO_SAY


def _sales_link_key(key: str) -> bool:
    lowered = key.lower()
    return "url" in lowered and any(
        word in lowered
        for word in ("upgrade", "checkout", "pricing", "topup", "billing", "subscri")
    )


def plain_result(value: Any, *, message: bool = False) -> Any:
    """A tool result with Audial's messages made plain and links to buy things dropped."""
    if isinstance(value, dict):
        return {
            k: plain_result(v, message=str(k).lower() in _MESSAGE_KEYS)
            for k, v in value.items()
            if not _sales_link_key(str(k))
        }
    if isinstance(value, (list, tuple)):
        return [plain_result(item, message=message) for item in value]
    if isinstance(value, str) and message:
        return plain_text(value)
    return value


def plain_usage(block: dict[str, Any]) -> dict[str, Any]:
    """The usage block without the plan's name, and with extra runs counted in `remaining`.

    A plan name and a separate count of bought runs invite the model to suggest buying more.
    """
    shown = {k: v for k, v in block.items() if k not in ("plan", "topupRemaining")}
    extra = block.get("topupRemaining")
    if isinstance(shown.get("remaining"), int) and isinstance(extra, int):
        shown["remaining"] += extra
    return plain_result(shown)
