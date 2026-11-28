"""Remote mode: the Audial tools over streamable HTTP, one Audial account per request.

`audial-mcp --http` serves MCP at `<public url>/mcp` for clients that cannot start a local
process (ChatGPT, web agents). It differs from the local (stdio) server in four ways:

- **Credentials come with each request.** Every request carries the caller's own Audial API key
  as `Authorization: Bearer aud_...`. The key is checked against the API once (and cached for a
  few minutes), and the SDK's credential lookups are pointed at the request being served
  (`install_sdk_credentials`). The server never uses a key of its own, from its environment or
  from a sign-in saved on the host.
- **Files arrive as links.** A remote caller has no files on this machine, so every file
  argument is an https URL (or a file object a client such as ChatGPT attaches, which carries
  one). The server downloads it into a private folder for the job, refusing anything that is
  not a public https address, and deletes it when the job ends. Local paths are refused.
- **Results leave as links.** Each job's output folder gets an unguessable name, and its files
  are served at `<public url>/files/<folder>/<name>` until they expire (24 h by default).
- **Long jobs outlive the call.** ChatGPT ends a tool call after about a minute, so a job runs
  in the background: the call waits up to `wait_s` and returns either the result or a job_id
  for the `check_job` tool (see `JobBoard`).

Nothing here changes the stdio server: the module only takes effect once `main()` is started
with `--http`.
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import socket
import sys
import tempfile
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlencode, urljoin, urlsplit

import anyio
import requests
from audial.api import device_login
from audial.api.exceptions import AudialAuthError
from audial.utils import config as sdk_config
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from audial_mcp import __version__
from audial_mcp.validation import ValidationError

log = logging.getLogger("audial_mcp")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
MCP_PATH = "/mcp"
HEALTH_PATH = "/healthz"
FILES_PATH = "/files"
DOMAIN_CHALLENGE_PATH = "/.well-known/openai-apps-challenge"
DEFAULT_RESULTS_TTL_S = 24 * 60 * 60
# How long one call waits for its job before returning a job_id instead. ChatGPT ends a tool
# call after about a minute, and progress notifications do not extend that.
DEFAULT_WAIT_S = 40

# The API refuses uploads over 100 MB, so a larger download could never be used.
MAX_INPUT_BYTES = 100 * 1024 * 1024
FETCH_TIMEOUT_S = 60
MAX_REDIRECTS = 3

# Audial API keys, as the API mints them (aud_<key id>_<secret>). Older keys need a separate
# user id, which a bearer token cannot carry, so they are not accepted here.
API_KEY_RE = re.compile(r"aud_[0-9a-f]{16}_[A-Za-z0-9_-]{32}")

# How long a checked key is trusted before the API is asked again. The API checks the key on
# every real call regardless; this only spares it a lookup on each MCP message.
KEY_CACHE_TTL_S = 300
KEY_CACHE_MAX = 10_000

# "Sign in with Audial" issues keys that expire after an hour (refreshed by the client) with one
# scope, "audial". A job runs on the key it started with for up to 15 minutes, so a key with
# less left than that is refused (401) and the client refreshes it first; otherwise the API
# would refuse the job's later calls and the job would fail. OAuth keys must live longer.
OAUTH_SCOPE = "audial"
OAUTH_MIN_LIFETIME_S = 15 * 60

# Result folder names: 32 url-safe characters from `secrets.token_urlsafe(24)`.
_FOLDER_RE = re.compile(r"[A-Za-z0-9_-]{32}")
_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")

# What a download without a usable extension in its URL is, by its Content-Type.
CONTENT_TYPES = {
    "audio/wav": ".wav",
    "audio/wave": ".wav",
    "audio/x-wav": ".wav",
    "audio/vnd.wave": ".wav",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/aiff": ".aiff",
    "audio/x-aiff": ".aiff",
    "audio/flac": ".flac",
    "audio/x-flac": ".flac",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/ogg": ".ogg",
    "audio/aac": ".aac",
    "audio/midi": ".mid",
    "audio/x-midi": ".mid",
    "audio/mid": ".mid",
    "application/json": ".json",
}


@dataclass(frozen=True)
class RemoteSettings:
    host: str
    port: int
    # Where clients reach this server, without a trailing slash: download links are built on
    # it, and so is the protected-resource metadata when an authorization server is set.
    public_url: str
    # The OAuth authorization server that issues Audial keys to clients, once the API has one.
    # When set, the server publishes RFC 9728 metadata naming it, so a client can sign the
    # user in by itself; when unset, callers bring an API key.
    authorization_server: str | None
    work_dir: Path
    results_ttl_s: int
    wait_s: int = DEFAULT_WAIT_S
    # The token OpenAI's domain verification asks the server's origin to serve, if set.
    domain_challenge: str | None = None
    # How much the results may take on disk before the oldest go early; None for no cap. On a
    # host whose disk is small and replaced when full (App Platform: 4 GiB), set it.
    max_results_bytes: int | None = None


def remote_settings(
    *,
    host: str | None,
    port: int | None,
    public_url: str | None,
    authorization_server: str | None,
    env: Mapping[str, str],
) -> RemoteSettings:
    """Settings for `--http`: command-line values win, then the environment, then defaults."""
    host = host or env.get("AUDIAL_MCP_HOST") or DEFAULT_HOST
    raw_port = port if port is not None else env.get("AUDIAL_MCP_PORT") or env.get("PORT")
    try:
        port = int(raw_port) if raw_port is not None else DEFAULT_PORT
        if not 0 < port < 65536:
            raise ValueError
    except ValueError:
        raise ValueError(f"the port must be a number from 1 to 65535, got {raw_port!r}") from None
    ttl = _positive(env, "AUDIAL_MCP_RESULTS_TTL_S", DEFAULT_RESULTS_TTL_S)
    wait = _positive(env, "AUDIAL_MCP_WAIT_S", DEFAULT_WAIT_S)
    public = (public_url or env.get("AUDIAL_MCP_PUBLIC_URL") or f"http://{host}:{port}").rstrip("/")
    issuer = (authorization_server or env.get("AUDIAL_MCP_AUTHORIZATION_SERVER") or "").strip()
    work = env.get("AUDIAL_MCP_WORK_DIR") or str(Path(tempfile.gettempdir()) / "audial-mcp")
    cap_mb = env.get("AUDIAL_MCP_MAX_RESULTS_MB")
    if cap_mb:
        try:
            cap = int(cap_mb) * 1024 * 1024
            if cap <= 0:
                raise ValueError
        except ValueError:
            raise ValueError(
                f"AUDIAL_MCP_MAX_RESULTS_MB must be a positive number of MB, got {cap_mb!r}"
            ) from None
    return RemoteSettings(
        host=host,
        port=port,
        public_url=public,
        authorization_server=issuer.rstrip("/") or None,
        work_dir=Path(work).expanduser(),
        results_ttl_s=ttl,
        wait_s=wait,
        domain_challenge=(env.get("AUDIAL_MCP_DOMAIN_CHALLENGE") or "").strip() or None,
        max_results_bytes=cap if cap_mb else None,
    )


def _positive(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name) or str(default)
    try:
        value = int(raw)
        if value <= 0:
            raise ValueError
    except ValueError:
        raise ValueError(f"{name} must be a positive number of seconds, got {raw!r}") from None
    return value


# --- who is calling -------------------------------------------------------------------------


def caller() -> AccessToken | None:
    """The verified credentials of the request being served, or None outside one."""
    return get_access_token()


def oauth_caller() -> bool:
    """Whether the request being served signed in with "Sign in with Audial" (ChatGPT, Claude).

    Such calls never count against the API's monthly or per-minute limits, unlike a user's own
    API key.
    """
    access = caller()
    return bool(access is not None and (access.claims or {}).get("oauth"))


# --- which client is calling ----------------------------------------------------------------
#
# The same server serves ChatGPT, Claude and anything else, and ChatGPT's rules forbid showing
# plans, upgrades or checkout links (see `audial_mcp.plain`). ChatGPT says who it is in three
# ways, any one of which is enough:
#
# - the OAuth client its token was issued to (from the API's whoami, once OAuth is live):
#   ChatGPT's client id is https://chatgpt.com/oauth/client.json, or a registered client named
#   "ChatGPT";
# - the `_meta` of every tool call: ChatGPT sends `openai/locale`, `openai/subject`,
#   `openai/session` and more (older clients `webplus/i18n`);
# - its HTTP user agent (`openai-mcp/...`).
#
# None of this is used to authorize anything. A client that pretends to be ChatGPT only gets
# plainer wording.

_CHATGPT_HOSTS = ("chatgpt.com", "openai.com")
_CHATGPT_META_PREFIXES = ("openai/", "webplus/")

_chatgpt: contextvars.ContextVar[bool] = contextvars.ContextVar("audial_mcp_chatgpt", default=False)


def _chatgpt_client(access: AccessToken | None) -> bool:
    if access is None:
        return False
    client_id = (access.client_id or "").strip().lower()
    host = (urlsplit(client_id).hostname or "") if "://" in client_id else ""
    if host in _CHATGPT_HOSTS or host.endswith(tuple(f".{h}" for h in _CHATGPT_HOSTS)):
        return True
    name = (access.claims or {}).get("client_name")
    return any(
        isinstance(v, str) and v.strip().lower() in ("chatgpt", "openai") for v in (client_id, name)
    )


def is_chatgpt(
    *,
    meta: Mapping[str, Any] | None,
    headers: Mapping[str, str] | None,
    access: AccessToken | None,
) -> bool:
    """Whether a tool call comes from ChatGPT, by its token's client, `_meta` or user agent."""
    if _chatgpt_client(access):
        return True
    if meta and any(str(key).startswith(_CHATGPT_META_PREFIXES) for key in meta):
        return True
    agent = ((headers or {}).get("user-agent") or "").lower()
    return "openai" in agent or "chatgpt" in agent


def chatgpt_caller() -> bool:
    """Whether the tool call being served comes from ChatGPT (set by `serving_call`)."""
    return _chatgpt.get()


@contextlib.contextmanager
def serving_call(context: Any) -> Iterator[None]:
    """Remember for the length of one tool call whether ChatGPT is the caller.

    `context` is the MCP SDK's tool `Context`: its request context carries the call's `_meta`
    and the HTTP request (with the user agent).
    """
    try:
        request_context = context.request_context if context is not None else None
    except ValueError:  # outside a request
        request_context = None
    request = getattr(request_context, "request", None)
    token = _chatgpt.set(
        is_chatgpt(
            meta=getattr(request_context, "meta", None),
            headers=getattr(request, "headers", None),
            access=caller(),
        )
    )
    try:
        yield
    finally:
        _chatgpt.reset(token)


def _same_resource(given: str, expected: str) -> bool:
    """`given` names this server: the MCP URL itself, or its origin, with or without a slash."""
    given = given.strip().rstrip("/")
    expected = expected.rstrip("/")
    origin = expected.removesuffix(MCP_PATH)
    return given in (expected, origin)


class AudialKeyVerifier:
    """Accept a bearer token that is a live Audial API key, and say whose it is.

    The access token's `subject` is the Audial user id and `claims["email"]` the account's
    email. A key the API refuses is not a valid token (401); an API that cannot be reached is
    an error (500), so a client does not mistake an outage for a revoked sign-in.

    A key minted by an OAuth sign-in (whoami's `token_type: "oauth"`) also says:
    - which client it was issued to (`client_id`, and `client_name` when the client registered
      one);
    - for which server (`resource`, the RFC 8707 resource indicator): one issued for another
      server is refused;
    - its `scope` and when it expires (`expires_at`, epoch seconds).

    A valid key is trusted for 5 minutes, never past its expiry, and one with less than
    `min_lifetime_s` left is refused so the client refreshes it. `forget` drops a key the API
    refused during a call, so the next request is checked again and gets the 401. A user's own
    API key (manual or device sign-in) has no audience, scope or expiry, and gets the full
    scope: it can do anything the account can.
    """

    def __init__(
        self,
        whoami: Callable[[str], dict[str, Any]] = device_login.whoami,
        *,
        resource: str | None = None,
        ttl_s: float = KEY_CACHE_TTL_S,
        max_entries: int = KEY_CACHE_MAX,
        min_lifetime_s: float = OAUTH_MIN_LIFETIME_S,
        clock: Callable[[], float] = time.time,
    ):
        self._whoami = whoami
        self._resource = resource
        self._ttl_s = ttl_s
        self._max = max_entries
        self._min_lifetime_s = min_lifetime_s
        self._clock = clock
        self._cache: OrderedDict[str, tuple[float, AccessToken]] = OrderedDict()

    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def forget(self, token: str) -> None:
        """Stop trusting `token` until the API has been asked about it again."""
        self._cache.pop(self._digest(token), None)

    def _usable(self, access: AccessToken) -> bool:
        if access.expires_at is None:
            return True
        return access.expires_at - self._clock() >= self._min_lifetime_s

    async def verify_token(self, token: str) -> AccessToken | None:
        if not API_KEY_RE.fullmatch(token):
            return None
        digest = self._digest(token)
        hit = self._cache.get(digest)
        if hit is not None and hit[0] > self._clock():
            if not self._usable(hit[1]):
                self._cache.pop(digest, None)
                return None
            self._cache.move_to_end(digest)
            return hit[1]
        self._cache.pop(digest, None)
        try:
            who = await anyio.to_thread.run_sync(self._whoami, token)
        except AudialAuthError:
            return None
        access = self._access(token, who)
        if access is None or not self._usable(access):
            return None
        until = self._clock() + self._ttl_s
        if access.expires_at is not None:
            until = min(until, access.expires_at)
        self._cache[digest] = (until, access)
        self._cache.move_to_end(digest)
        while len(self._cache) > self._max:
            self._cache.popitem(last=False)
        return access

    def _access(self, token: str, who: Any) -> AccessToken | None:
        """The access token whoami's answer describes, or None if it is not one for us."""
        user_id = who.get("user_id") if isinstance(who, dict) else None
        if not isinstance(user_id, str) or not user_id:
            return None
        oauth = who.get("token_type") == "oauth"
        resource = _text(who.get("resource"))
        if oauth and resource and self._resource and not _same_resource(resource, self._resource):
            log.info("refused a key issued for another server")
            return None
        expires_at = who.get("expires_at") if oauth else None
        if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
            expires_at = None
        scope = _text(who.get("scope")) if oauth else None
        return AccessToken(
            token=token,
            client_id=_text(who.get("client_id")) or "audial-api-key",
            scopes=scope.split() if scope else [OAUTH_SCOPE],
            expires_at=int(expires_at) if expires_at is not None else None,
            resource=resource,
            subject=user_id,
            claims={
                "email": _text(who.get("email")),
                "client_name": _text(who.get("client_name")),
                "oauth": oauth,
            },
        )


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _request_api_key() -> str | None:
    access = caller()
    return access.token if access is not None else None


def _request_user_id(api_key: str | None = None) -> str | None:
    access = caller()
    if access is None or (api_key is not None and api_key != access.token):
        return None
    return access.subject


def install_sdk_credentials() -> Callable[[], None]:
    """Make the SDK use the calling request's key and user id. Returns a function that undoes it.

    The SDK reads its credentials from process-wide places (`AUDIAL_API_KEY`, the sign-in
    file), and three of its functions look the user id up without the key they were given. A
    server shared by many users must never let one caller's job run on another's account, so
    both lookups are replaced, in every SDK module that holds a reference to them, by versions
    that read the request being served. Outside a request they return None, and the SDK then
    refuses to run ("not signed in"); they never fall back to the host's own credentials.
    """
    swaps = (
        (sdk_config.get_api_key, _request_api_key),
        (sdk_config.get_user_id, _request_user_id),
    )
    patched: list[tuple[Any, str, Any]] = []
    for name, module in list(sys.modules.items()):
        if module is None or not (name == "audial" or name.startswith("audial.")):
            continue
        for attr, value in list(vars(module).items()):
            for original, replacement in swaps:
                if value is original:
                    patched.append((module, attr, original))
                    setattr(module, attr, replacement)

    def restore() -> None:
        for module, attr, original in patched:
            setattr(module, attr, original)

    return restore


# --- files in --------------------------------------------------------------------------------

_inputs_dir: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "audial_mcp_inputs_dir", default=None
)


def file_arg(value: Any) -> Any:
    """Pydantic before-validator for file arguments: a file object becomes its link.

    ChatGPT passes a file the user attached as `{download_url, file_id, mime_type, file_name}`.
    The tools take strings, so the object becomes its `download_url`, with the name and type
    (which the download may not reveal) carried in the URL's fragment. A fragment is never sent
    to the server, so the link fetches the same file. Anything else passes through unchanged.
    """
    if not isinstance(value, dict):
        return value
    url = value.get("download_url")
    if not isinstance(url, str) or not url:
        raise ValueError("a file object needs a download_url")
    hints = {
        key: value[field]
        for key, field in (("name", "file_name"), ("type", "mime_type"))
        if isinstance(value.get(field), str) and value[field]
    }
    base = url.split("#", 1)[0]
    return f"{base}#{urlencode(hints)}" if hints else base


def _hints(url: str) -> tuple[str, dict[str, str]]:
    """Split the hints `file_arg` put in a URL's fragment from the URL itself."""
    base, _, fragment = url.partition("#")
    hints = {k: v[0] for k, v in parse_qs(fragment).items() if k in ("name", "type") and v}
    return base, hints


def _check_public_host(host: str) -> None:
    """Refuse a host that resolves to anything but public addresses (no SSRF into the host's
    network: metadata endpoints, localhost, private ranges)."""
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError):
        raise ValidationError(f"{host} could not be found. Check the link.") from None
    for info in infos:
        address = ipaddress.ip_address(str(info[4][0]).split("%", 1)[0])  # no IPv6 zone id
        mapped = getattr(address, "ipv4_mapped", None)
        if not (mapped or address).is_global:
            raise ValidationError(f"{host} is not a public address, so it cannot be fetched.")


def _check_url(url: str) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme != "https" or not parts.hostname:
        raise ValidationError(
            "Give the file as an https URL (a link to the file, or a file attached to the chat). "
            "This server cannot read paths on anyone's computer."
        )
    if parts.username or parts.password:
        raise ValidationError("Give a link without a user name or password in it.")
    _check_public_host(parts.hostname)
    return parts.geturl()


def _filename(
    url: str, response: requests.Response, kinds: frozenset[str], hints: Mapping[str, str]
) -> str:
    """A safe local name for a download, with the extension the SDK needs to tell its kind.

    From the attached file's name, the response's, or the link's; failing those, from the type
    the client gave or the response's Content-Type.
    """
    disposition = response.headers.get("content-disposition", "")
    match = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", disposition, re.IGNORECASE)
    candidates = [
        hints.get("name", ""),
        unquote(match.group(1)) if match else "",
        unquote(urlsplit(url).path),
    ]
    for candidate in candidates:
        name = Path(candidate).name
        if Path(name).suffix.lower() in kinds:
            stem = _NAME_RE.sub("_", Path(name).stem).strip("._") or "input"
            return f"{stem[:60]}{Path(name).suffix.lower()}"
    for content_type in (hints.get("type", ""), response.headers.get("content-type", "")):
        suffix = CONTENT_TYPES.get(content_type.split(";")[0].strip().lower())
        if suffix in kinds:
            return f"input{suffix}"
    allowed = ", ".join(sorted(kinds))
    raise ValidationError(
        f"The link does not look like a supported file (expected {allowed}). "
        "Link to the file itself, not to a page about it."
    )


def fetch(
    value: Any,
    kinds: frozenset[str],
    label: str,
    *,
    max_bytes: int = MAX_INPUT_BYTES,
) -> Path:
    """Download one file argument into the current call's input folder and return its path.

    Blocking: call it from a worker thread. Redirects are followed by hand so that every hop
    is checked against `_check_public_host`, and the download stops at `max_bytes`.
    """
    folder = _inputs_dir.get()
    if folder is None:
        raise RuntimeError("fetch() called outside Remote.use_inputs()")
    if not isinstance(value, str):
        raise ValidationError(f"Give the {label} as an https link or an attached file.")
    url, hints = _hints(value.strip())
    url = _check_url(url)
    with requests.Session() as session:
        for _ in range(MAX_REDIRECTS + 1):
            response = session.get(url, stream=True, allow_redirects=False, timeout=FETCH_TIMEOUT_S)
            if response.is_redirect and response.headers.get("location"):
                response.close()
                url = _check_url(urljoin(url, response.headers["location"]))
                continue
            break
        else:
            raise ValidationError("The link redirects too many times.")
        with response:
            if response.status_code != 200:
                raise ValidationError(
                    f"Fetching the {label} failed (HTTP {response.status_code}). Check that the "
                    "link works without signing in."
                )
            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise ValidationError(f"The {label} is over {max_bytes // (1024 * 1024)} MB.")
            target = folder / _filename(url, response, kinds, hints)
            n = 1
            while target.exists():
                target = target.with_name(f"{target.stem}-{n}{target.suffix}")
                n += 1
            size = 0
            with open(target, "wb") as out:
                for chunk in response.iter_content(chunk_size=1 << 16):
                    size += len(chunk)
                    if size > max_bytes:
                        out.close()
                        target.unlink(missing_ok=True)
                        raise ValidationError(
                            f"The {label} is over {max_bytes // (1024 * 1024)} MB."
                        )
                    out.write(chunk)
    if size == 0:
        target.unlink(missing_ok=True)
        raise ValidationError(f"The {label} link returned an empty file.")
    return target


# --- results out -----------------------------------------------------------------------------


class ResultStore:
    """Job folders with unguessable names, served as download links until they expire.

    With `max_bytes`, the oldest results also go once the folders together pass it.
    """

    def __init__(self, root: Path, public_url: str, ttl_s: int, max_bytes: int | None = None):
        root.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(root, 0o700)
        self.root = root.resolve()
        self.base = f"{public_url}{FILES_PATH}"
        self.ttl_s = ttl_s
        self.max_bytes = max_bytes

    def new_job_dir(self) -> Path:
        self.sweep()
        folder = self.root / secrets.token_urlsafe(24)
        folder.mkdir()
        return folder

    def url_for(self, path: Path | str) -> str:
        rel = Path(path).resolve().relative_to(self.root)
        return f"{self.base}/{quote(rel.as_posix())}"

    def sweep(self) -> None:
        """Delete folders older than the TTL, then the oldest finished ones while over the cap.

        Runs on each new job, so storage stays bounded. A folder with nothing in it belongs to
        a job still running (results arrive when it ends), so the cap never takes one.
        """
        cutoff = time.time() - self.ttl_s
        fresh: list[tuple[float, Path]] = []
        for folder in self.root.iterdir():
            with contextlib.suppress(OSError):
                if not folder.is_dir():
                    continue
                modified = folder.stat().st_mtime
                if modified < cutoff:
                    shutil.rmtree(folder, ignore_errors=True)
                else:
                    fresh.append((modified, folder))
        if self.max_bytes is None:
            return
        sizes = {folder: _size(folder) for _modified, folder in fresh}
        total = sum(sizes.values())
        removed = 0
        for _modified, folder in sorted(fresh):
            if total <= self.max_bytes:
                break
            if sizes[folder]:
                shutil.rmtree(folder, ignore_errors=True)
                total -= sizes[folder]
                removed += 1
        if removed:
            log.warning("results over their cap: removed %d folder(s) before they expired", removed)

    def _folder(self, name: str) -> Path | None:
        if not _FOLDER_RE.fullmatch(name):
            return None
        folder = self.root / name
        try:
            fresh = folder.is_dir() and folder.stat().st_mtime >= time.time() - self.ttl_s
        except OSError:
            return None
        return folder if fresh else None

    async def serve(self, request: Request) -> Response:
        """GET /files/<folder>[/<path>]: one file, or the folder's list of links."""
        name = request.path_params.get("folder", "")
        folder = self._folder(name)
        if folder is None:
            return JSONResponse({"error": "This link has expired or does not exist."}, 404)
        files = _visible_files(folder)
        rel = request.path_params.get("path", "")
        if not rel:
            return JSONResponse({"files": [self.url_for(p) for p in files]})
        target = (folder / rel).resolve()
        if target not in files:
            return JSONResponse({"error": "No such file."}, 404)
        return FileResponse(target, filename=target.name, content_disposition_type="attachment")


def _size(folder: Path) -> int:
    total = 0
    for path in folder.rglob("*"):
        with contextlib.suppress(OSError):
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
    return total


def _visible_files(folder: Path) -> list[Path]:
    """The files a job folder serves: everything but hidden files and folders. Resolved."""
    folder = folder.resolve()
    return [
        p
        for p in sorted(folder.rglob("*"))
        if p.is_file()
        and not p.is_symlink()
        and not any(part.startswith(".") for part in p.relative_to(folder).parts)
    ]


# --- jobs that outlive the request ----------------------------------------------------------


@dataclass
class RemoteJob:
    """One job running (or run) in the background for one account."""

    id: str
    owner: str
    tool: str
    output_dir: Path
    relay: Any
    made: dict[str, str]
    started: float
    deadline: float
    done: threading.Event = field(default_factory=threading.Event)
    finished: float | None = None
    # The key the job runs on, to stop trusting it if the API refuses it mid-job.
    token: str = ""
    raw: Any = None
    error: BaseException | None = None
    # The finished JobResult, built once on the first call that sees the job done.
    result: Any = None


class JobBoard:
    """The jobs this process is running or has run, by id, for `check_job`.

    In memory: a restart forgets them (their results stay downloadable until they expire), and
    a job can only be checked on the replica that started it.
    """

    def __init__(self, ttl_s: int):
        self.ttl_s = ttl_s
        self._jobs: dict[str, RemoteJob] = {}
        self._lock = threading.Lock()

    def start(
        self,
        *,
        owner: str,
        tool: str,
        output_dir: Path,
        fn: Callable[[], Any],
        relay: Any,
        made: dict[str, str],
        timeout_s: float,
        cleanup: Callable[[], Any],
        token: str = "",
    ) -> RemoteJob:
        """Run `fn` in its own thread, in a copy of the caller's context (so the SDK sees the
        caller's credentials), and return the job at once."""
        now = time.monotonic()
        job = RemoteJob(
            id=output_dir.name,
            owner=owner,
            tool=tool,
            output_dir=output_dir,
            relay=relay,
            made=made,
            started=now,
            deadline=now + timeout_s,
            token=token,
        )
        context = contextvars.copy_context()

        def run() -> None:
            try:
                job.raw = context.run(fn)
            except BaseException as exc:  # noqa: BLE001 — reported to whoever checks the job
                job.error = exc
            finally:
                with contextlib.suppress(Exception):
                    cleanup()
                job.finished = time.monotonic()
                job.done.set()

        with self._lock:
            self._sweep(now)
            self._jobs[job.id] = job
        threading.Thread(target=run, name=f"audial-{tool}", daemon=True).start()
        return job

    def get(self, job_id: str, owner: str | None) -> RemoteJob | None:
        """The job, if it exists and belongs to `owner`; another account's job does not exist."""
        with self._lock:
            job = self._jobs.get(job_id) if isinstance(job_id, str) else None
        if job is None or owner is None or not secrets.compare_digest(job.owner, owner):
            return None
        return job

    def _sweep(self, now: float) -> None:
        for job_id, job in list(self._jobs.items()):
            if job.finished is not None and job.finished < now - self.ttl_s:
                del self._jobs[job_id]


@dataclass
class Remote:
    """The live remote-mode state: settings, inputs, results, jobs, and how to scrub text."""

    settings: RemoteSettings
    results: ResultStore
    inputs_root: Path
    jobs: JobBoard
    restore_sdk: Callable[[], None] | None = None
    # The stdio server's settings before remote mode replaced them (restored by tests).
    saved_settings: Any = None
    # The token verifier in use (see `forget`).
    verifier: Any = None

    def forget(self, token: str | None) -> None:
        """The API refused `token`: stop trusting it, so the next request gets a 401."""
        forget = getattr(self.verifier, "forget", None)
        if token and forget is not None:
            forget(token)

    @classmethod
    def create(cls, settings: RemoteSettings) -> Remote:
        work = settings.work_dir
        inputs = work / "inputs"
        inputs.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(work, 0o700)
        results = ResultStore(
            work / "results",
            settings.public_url,
            settings.results_ttl_s,
            max_bytes=settings.max_results_bytes,
        )
        # Inputs left behind by a process that stopped mid-job are of no use to anyone.
        cutoff = time.time() - settings.results_ttl_s
        for folder in inputs.iterdir():
            with contextlib.suppress(OSError):
                if folder.stat().st_mtime < cutoff:
                    shutil.rmtree(folder, ignore_errors=True)
        return cls(
            settings=settings,
            results=results,
            inputs_root=inputs.resolve(),
            jobs=JobBoard(settings.results_ttl_s),
        )

    def open_inputs(self) -> Path:
        """A private folder for one job's downloaded inputs; `close_inputs` deletes it."""
        folder = self.inputs_root / secrets.token_urlsafe(16)
        folder.mkdir()
        return folder

    @contextlib.contextmanager
    def use_inputs(self, folder: Path) -> Iterator[Path]:
        """Make `fetch` download into `folder` (also from worker threads started inside)."""
        token = _inputs_dir.set(folder)
        try:
            yield folder
        finally:
            _inputs_dir.reset(token)

    def close_inputs(self, folder: Path) -> None:
        shutil.rmtree(folder, ignore_errors=True)

    def scrub(self, text: str) -> str:
        """Server paths out of text bound for a caller: results become links, inputs their name."""
        for root in {str(self.results.root), str(self.settings.work_dir / "results")}:
            text = text.replace(root, self.results.base)
        for root in {str(self.inputs_root), str(self.settings.work_dir / "inputs")}:
            text = re.sub(re.escape(root) + r"/[A-Za-z0-9_-]+/", "", text)
        return text


LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")

# The authorization server the protected-resource metadata names is required by the SDK's
# settings model even when none is published; this placeholder is never sent anywhere.
_UNPUBLISHED_ISSUER = "https://api.audialmusic.ai"


def build_app(server: Any, remote: Remote, verifier: TokenVerifier) -> Starlette:
    """The ASGI app: MCP at /mcp behind bearer auth, plus the download links and a health check.

    Stateless streamable HTTP: every POST is complete in itself, so any replica can serve any
    request and nothing has to stick to a session. Progress notifications still stream back on
    the POST's own response while a job runs.
    """
    settings = remote.settings
    issuer = settings.authorization_server
    auth = AuthSettings(
        issuer_url=issuer or _UNPUBLISHED_ISSUER,
        # With an authorization server, publish RFC 9728 metadata so a client can find it from
        # the 401's WWW-Authenticate header; without one there is nothing to point at.
        resource_server_url=f"{settings.public_url}{MCP_PATH}" if issuer else None,
        validate_token_resource=False if issuer else None,
        # Published as the metadata's `scopes_supported` (Claude asks for what it lists) and
        # required of every token. The verifier gives a user's own API key the scope too.
        required_scopes=[OAUTH_SCOPE] if issuer else None,
    )
    public_host = urlsplit(settings.public_url).hostname or ""
    # Host/Origin checks only help a server that is reached on localhost; behind a public URL
    # the bearer key is the protection, and an Origin check could turn away a real client.
    local = public_host in LOCAL_HOSTS or public_host.strip("[]") in LOCAL_HOSTS
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=local,
        allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", "127.0.0.1", "localhost"],
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"],
    )

    async def health(_request: Request) -> Response:
        return JSONResponse({"ok": True, "version": __version__})

    serve, methods = remote.results.serve, ["GET", "HEAD"]
    routes = [
        Route(HEALTH_PATH, health, methods=["GET"]),
        Route(f"{FILES_PATH}/{{folder}}", serve, methods=methods),
        Route(f"{FILES_PATH}/{{folder}}/{{path:path}}", serve, methods=methods),
    ]
    if settings.domain_challenge:
        # Publishing an app in ChatGPT starts with proving the server's origin is ours: the
        # submission portal gives a token, and the origin serves it here as plain text.
        token = settings.domain_challenge

        async def challenge(_request: Request) -> Response:
            return PlainTextResponse(token)

        routes.append(Route(DOMAIN_CHALLENGE_PATH, challenge, methods=["GET"]))
    return server._lowlevel_server.streamable_http_app(  # noqa: SLF001 (no public way to pass auth)
        streamable_http_path=MCP_PATH,
        stateless_http=True,
        transport_security=security,
        host=settings.host,
        auth=auth,
        token_verifier=verifier,
        custom_starlette_routes=routes,
    )


def describe(settings: RemoteSettings) -> str:
    """One startup line for the log."""
    auth = (
        f"OAuth via {settings.authorization_server}"
        if settings.authorization_server
        else "Audial API key per request (Authorization: Bearer)"
    )
    return json.dumps(
        {
            "mcp": f"{settings.public_url}{MCP_PATH}",
            "listen": f"{settings.host}:{settings.port}",
            "auth": auth,
            "results_ttl_s": settings.results_ttl_s,
        }
    )
