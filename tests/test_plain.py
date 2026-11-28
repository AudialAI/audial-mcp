"""What ChatGPT may be told when an account is out of runs: plainly that, and nothing else.

OpenAI's plugin rules: a plugin "must not display subscription plans, initiate new
subscriptions, or promote upgrades", and must not link to checkout. The server is the same for
every client, so it decides per request whether the caller is ChatGPT (`remote.is_chatgpt`)
and, if so, words refusals with `audial_mcp.plain`.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest
from audial.api.exceptions import AudialError, AudialLimitReached, SubscriptionRequiredError
from mcp.server.auth.provider import AccessToken

from audial_mcp import plain
from audial_mcp import remote as remote_mode
from audial_mcp.errors import REMOTE_SUBSCRIPTION_REQUIRED, to_tool_error

# Words and links no ChatGPT-bound refusal may carry.
SALES = re.compile(
    r"subscri|upgrad|pricing|price|checkout|top[- ]?up|trial|purchas|\bbuy\b|\bplans?\b|https?://",
    re.IGNORECASE,
)

RESETS = datetime(2026, 12, 1, tzinfo=timezone.utc)
PIPELINE_REFUSED = AudialError(
    "Pipeline sample-pack failed at step stems: This feature needs an active Audial "
    "subscription. Subscribe at audialmusic.ai and try again. (SUBSCRIPTION_REQUIRED)"
)


def limit(upgrade_url="https://audialmusic.ai/product/pricing"):
    return AudialLimitReached(limit=100, used=100, resets_at=RESETS, upgrade_url=upgrade_url)


# --- ChatGPT's wording ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [SubscriptionRequiredError(), SubscriptionRequiredError("Subscribe now!"), PIPELINE_REFUSED],
)
def test_out_of_runs_says_so_plainly_for_chatgpt(exc):
    text = str(to_tool_error(exc, tool="stem_split", remote=True, chatgpt=True))
    assert text == plain.NO_RUNS_LEFT
    assert "no runs left" in text and not SALES.search(text)


def test_the_monthly_limit_says_when_runs_come_back_without_a_link_for_chatgpt():
    text = str(to_tool_error(limit(), tool="master", remote=True, chatgpt=True))
    assert "100 of 100" in text and "2026-12-01" in text and "will not help" in text
    assert not SALES.search(text), text
    bare = str(to_tool_error(AudialLimitReached(), tool="master", remote=True, chatgpt=True))
    assert "this month's runs" in bare and not SALES.search(bare)


def test_any_other_chatgpt_error_loses_sales_sentences_and_purchase_links():
    exc = AudialError(
        "The file could not be decoded. Upgrade to Studio for longer files. "
        "More calls: https://audialmusic.ai/product/pricing"
    )
    text = str(to_tool_error(exc, tool="analyze", remote=True, chatgpt=True))
    assert text.startswith("The file could not be decoded.")
    assert not SALES.search(text), text


def test_plain_text_keeps_ordinary_errors_whole():
    message = "The audio file is over 100 MB. Trim it and try again."
    assert plain.plain_text(message) == message
    assert plain.plain_text("Subscribe at audialmusic.ai.") == plain.NO_RUNS_LEFT
    assert plain.plain_text("Buy more calls.") == plain.NOTHING_TO_SAY


def test_plain_result_scrubs_messages_and_drops_purchase_links_but_not_content():
    status = {
        "state": "failed",
        "error": {"message": PIPELINE_REFUSED.args[0], "code": "SUBSCRIPTION_REQUIRED"},
        "steps": [{"name": "stems", "error": "Subscribe at audialmusic.ai and try again."}],
        "upgradeUrl": "https://audialmusic.ai/product/pricing",
        # What a track says is the user's content, not Audial's words: never touched.
        "lines": [{"start": 1.0, "end": 2.0, "text": "I can't buy your love, subscribe to me"}],
        "description": "An upbeat pop track.",
    }
    clean = plain.plain_result(status)
    assert "upgradeUrl" not in clean
    assert clean["error"]["message"] == plain.NO_RUNS_LEFT
    assert clean["steps"][0]["error"] == plain.NO_RUNS_LEFT
    assert clean["lines"] == status["lines"] and clean["description"] == status["description"]
    assert clean["error"]["code"] == "SUBSCRIPTION_REQUIRED"  # a code is not wording


def test_chatgpt_usage_folds_extra_runs_in_and_names_no_plan():
    block = {
        "plan": "creator",
        "monthlyLimit": 100,
        "used": 100,
        "remaining": 0,
        "topupRemaining": 20,
        "resetsAt": 1798000000000,
    }
    shown = plain.plain_usage(block)
    assert shown == {"monthlyLimit": 100, "used": 100, "remaining": 20, "resetsAt": 1798000000000}
    assert plain.plain_usage({"used": 1}) == {"used": 1}


# --- everyone else keeps their wording -----------------------------------------------------------


def test_claude_and_other_remote_callers_keep_the_remote_wording():
    assert str(to_tool_error(SubscriptionRequiredError(), tool="x", remote=True)) == (
        REMOTE_SUBSCRIPTION_REQUIRED
    )
    # A pipeline refused at the gate now reads the same as any other refusal.
    assert str(to_tool_error(PIPELINE_REFUSED, tool="x", remote=True)) == (
        REMOTE_SUBSCRIPTION_REQUIRED
    )
    text = str(to_tool_error(limit(), tool="x", remote=True))
    assert text.startswith("Monthly API limit reached (100/100).") and "http" not in text


def test_local_users_keep_the_sdk_wording_with_its_links():
    assert "Subscribe at audialmusic.ai" in str(
        to_tool_error(SubscriptionRequiredError(), tool="x")
    )
    assert "https://audialmusic.ai/product/pricing" in str(to_tool_error(limit(), tool="x"))
    assert "Subscribe at audialmusic.ai" in str(to_tool_error(PIPELINE_REFUSED, tool="x"))


# --- telling ChatGPT apart -----------------------------------------------------------------------


def token(client_id="audial-api-key", **claims):
    return AccessToken(token="t", client_id=client_id, scopes=[], subject="u", claims=claims)


@pytest.mark.parametrize(
    "meta, headers, access",
    [
        ({"openai/subject": "v1/abc"}, {}, None),
        ({"openai/locale": "en-US"}, None, None),
        ({"webplus/i18n": "en-US"}, None, None),  # what older ChatGPT clients send
        (None, {"user-agent": "openai-mcp/1.0.0"}, None),
        (None, {}, token("https://chatgpt.com/oauth/client.json")),
        (None, {}, token("dcr-123", client_name="ChatGPT")),
    ],
)
def test_chatgpt_is_recognised_by_any_of_its_signals(meta, headers, access):
    assert remote_mode.is_chatgpt(meta=meta, headers=headers, access=access)


@pytest.mark.parametrize(
    "meta, headers, access",
    [
        (None, None, None),
        ({"progressToken": 1}, {"user-agent": "Claude-User"}, token()),
        (
            {},
            {},
            token("https://claude.ai/oauth/claude-code-client-metadata", client_name="Claude"),
        ),
        ({"io.modelcontextprotocol/related-task": {}}, {"user-agent": "python-httpx/0.28"}, None),
    ],
)
def test_other_clients_are_not_taken_for_chatgpt(meta, headers, access):
    assert not remote_mode.is_chatgpt(meta=meta, headers=headers, access=access)
