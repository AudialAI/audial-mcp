from audial.api.exceptions import AudialAPIError, AudialAuthError, SubscriptionRequiredError
from mcp.server.mcpserver.exceptions import ToolError

from audial_mcp.config import ConfigError
from audial_mcp.errors import SignInRequired, to_tool_error
from audial_mcp.jobs import JobTimeout
from audial_mcp.validation import ValidationError


def test_subscription_error_keeps_api_text():
    err = to_tool_error(
        SubscriptionRequiredError("This feature needs an active Audial subscription."),
        tool="sound2vital",
    )
    assert isinstance(err, ToolError) and "subscription" in str(err)


def test_auth_error_with_a_configured_key_points_at_the_variables():
    err = to_tool_error(AudialAuthError("401"), tool="analyze", api_key_from_env=True)
    assert "AUDIAL_USER_ID" in str(err) and "AUDIAL_API_KEY" in str(err)


def test_auth_error_with_a_browser_sign_in_says_to_sign_in_again():
    err = to_tool_error(AudialAuthError("401"), tool="analyze")
    assert "sign_in" in str(err) and "AUDIAL_API_KEY" not in str(err)


def test_sign_in_required_carries_the_link_and_code():
    err = to_tool_error(SignInRequired("https://x/activate?code=BCDF-GHJK", "BCDF-GHJK"), tool="x")
    assert "https://x/activate?code=BCDF-GHJK" in str(err) and "BCDF-GHJK" in str(err)


def test_config_and_validation_pass_through():
    assert "hint" in str(to_tool_error(ConfigError("hint"), tool="x"))
    assert "bad path" in str(to_tool_error(ValidationError("bad path"), tool="x"))


def test_timeout_mentions_execution_id():
    err = to_tool_error(JobTimeout("master", 900), tool="master", execution_id="-P2h")
    assert "900" in str(err) and "-P2h" in str(err) and "still" in str(err)


def test_generic_api_error_verbatim_and_unknown_is_generic():
    assert "quota" in str(to_tool_error(AudialAPIError("quota exceeded"), tool="x"))
    msg = str(to_tool_error(RuntimeError("secret internals sk-123"), tool="x"))
    assert "sk-123" not in msg and "x failed unexpectedly" in msg
