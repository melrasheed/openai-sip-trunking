"""The Azure AI Search knowledge base, wired in as a remote MCP tool.

Every Azure AI Search knowledge base is itself an MCP server exposing a single
`knowledge_base_retrieve` tool:

    https://<service>.search.windows.net/knowledgebases/<name>/mcp?api-version=<v>

The Realtime API can call that server directly — it is the service, not this
application, that performs the tool call, so there is no `function_call_output`
plumbing here. All this module does is describe the server, hand over the
credential, and tell the agent when to reach for it.

    https://learn.microsoft.com/azure/search/agentic-retrieval-how-to-retrieve
    https://developers.openai.com/api/docs/guides/realtime-mcp?api=realtime

Everything is read from the environment at call time rather than at import
time, because `app.py` imports its modules before `load_dotenv()` runs.
"""

import json
import os
import re

import requests

# The only tool a knowledge base exposes, and therefore the only one this agent
# is ever allowed to call.
TOOL_NAME = "knowledge_base_retrieve"

# 2026-08-01-preview returns synthesized answers when the knowledge base has an
# LLM configured, which suits a phone call. 2026-04-01 is extractive only and
# returns grounding data the agent would have to summarise itself.
DEFAULT_API_VERSION = "2026-08-01-preview"

DEFAULT_TOPIC = (
    "CBQ — Commercial Bank of Qatar products, services, fees, policies, "
    "branches and procedures"
)

# server_label is an identifier, not free text.
_LABEL_SAFE = re.compile(r"[^A-Za-z0-9_-]+")


def _env(name, default=""):
    return (os.environ.get(name) or default).strip()


def endpoint():
    """Search service endpoint, no trailing slash."""
    return _env("AZURE_SEARCH_ENDPOINT").rstrip("/")


def knowledge_base_name():
    return _env("AZURE_SEARCH_KNOWLEDGE_BASE")


def api_key():
    return _env("AZURE_SEARCH_API_KEY")


def api_version():
    return _env("AZURE_SEARCH_API_VERSION", DEFAULT_API_VERSION)


def mcp_url():
    """The knowledge base's MCP endpoint, or "" when nothing is configured.

    `AZURE_SEARCH_MCP_URL` wins outright, for a knowledge base fronted by a
    gateway or hosted somewhere the endpoint/name pair cannot describe.
    """
    override = _env("AZURE_SEARCH_MCP_URL")
    if override:
        return override

    host, name = endpoint(), knowledge_base_name()
    if not host or not name:
        return ""
    return f"{host}/knowledgebases/{name}/mcp?api-version={api_version()}"


def server_label():
    """Identifier the agent sees for this server; the events echo it back."""
    override = _env("AZURE_SEARCH_MCP_SERVER_LABEL")
    label = _LABEL_SAFE.sub("_", override or knowledge_base_name() or "knowledge_base")
    return label.strip("_") or "knowledge_base"


def is_configured():
    """True when there is both somewhere to call and something to call with."""
    return bool(mcp_url() and api_key())


def build_tool():
    """The `mcp` entry for the session's `tools` array, or None.

    `require_approval` is "never" on purpose: an approval round-trip would mean
    dead air on a live call. The risk is bounded instead by `allowed_tools`,
    which narrows the surface to the one read-only retrieval tool.
    """
    if not is_configured():
        return None

    return {
        "type": "mcp",
        "server_label": server_label(),
        "server_url": mcp_url(),
        # Admin key on the `api-key` header. A bearer token in `authorization`
        # is the recommended production alternative; see the README.
        "headers": {"api-key": api_key()},
        "allowed_tools": [TOOL_NAME],
        "require_approval": "never",
    }


def redact(value):
    """Deep-copies a payload with every MCP header value masked.

    The console returns the whole accept body from `/api/preview`, and the
    search key lives in it. Nothing that leaves this process may carry it.
    """
    if isinstance(value, dict):
        return {
            key: {inner: "***redacted***" for inner in item}
            if key == "headers" and isinstance(item, dict)
            else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def directive(topic=""):
    """The `{knowledge}` section of the system prompt, or "" when unconfigured.

    Names the knowledge base explicitly: the agent sees the same name on the
    `server_label` of the tool it is being told to use, so the instruction and
    the tool it refers to are unambiguous.
    """
    if not is_configured():
        return ""

    name = knowledge_base_name() or server_label()
    subject = (topic or "").strip() or DEFAULT_TOPIC

    return (
        f'Use the "{name}" knowledge base for any question related to {subject}. '
        "Search it before saying you do not know, and answer only from what it "
        "returns — never invent a fee, a rate, a policy or a procedure. If it "
        "comes back with nothing, say so plainly and offer to follow up.\n"
        "The search takes a few seconds. Say briefly that you are looking it up, "
        "then give the answer as soon as it arrives. Never end the call or wait "
        "for the caller to ask again, and never offer to follow up later while "
        "the search is still running.\n"
        "You give short and to the point answers."
    )


# ---------------------------------------------------------------------------
# Connectivity check
# ---------------------------------------------------------------------------


def _parse_mcp_response(text):
    """Reads one JSON-RPC result out of a JSON or SSE response body.

    The search service answers with `text/event-stream` even for a single
    request, so the payload arrives as `data: {...}` lines.
    """
    text = (text or "").strip()
    if not text:
        return None

    if not text.startswith("event:") and not text.startswith("data:"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None

    for line in text.splitlines():
        if line.startswith("data:"):
            try:
                return json.loads(line[len("data:") :].strip())
            except json.JSONDecodeError:
                continue
    return None


def _rpc(url, key, method, params, timeout):
    response = requests.post(
        url,
        headers={
            "api-key": key,
            "Content-Type": "application/json",
            # The server negotiates SSE, so both must be advertised.
            "Accept": "application/json, text/event-stream",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=timeout,
    )
    response.raise_for_status()
    return _parse_mcp_response(response.text) or {}


def probe(timeout=15):
    """Handshakes with the knowledge base and lists its tools.

    Used by the console's Test button so a bad key or a mistyped name is found
    before a caller does. Returns a dict the UI renders as-is.
    """
    if not mcp_url():
        return {"ok": False, "error": "No knowledge base configured. Set AZURE_SEARCH_ENDPOINT and AZURE_SEARCH_KNOWLEDGE_BASE."}
    if not api_key():
        return {"ok": False, "error": "No search key configured. Set AZURE_SEARCH_API_KEY."}

    url, key = mcp_url(), api_key()
    try:
        handshake = _rpc(
            url,
            key,
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "sip-voice-agent", "version": "1.0"},
            },
            timeout,
        )
        listing = _rpc(url, key, "tools/list", {}, timeout)
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        detail = (exc.response.text[:200] if exc.response is not None else str(exc)).strip()
        hint = " Check AZURE_SEARCH_API_KEY." if status in (401, 403) else ""
        return {"ok": False, "error": f"HTTP {status} from the knowledge base.{hint} {detail}".strip()}
    except requests.RequestException as exc:
        return {"ok": False, "error": f"Could not reach the knowledge base: {exc}"}

    error = (handshake.get("error") or listing.get("error") or {}).get("message")
    if error:
        return {"ok": False, "error": error}

    tools = [
        tool.get("name")
        for tool in (listing.get("result") or {}).get("tools") or []
        if tool.get("name")
    ]
    server = ((handshake.get("result") or {}).get("serverInfo") or {}).get("name", "")

    if TOOL_NAME not in tools:
        return {
            "ok": False,
            "server": server,
            "tools": tools,
            "error": f"The server answered but does not expose {TOOL_NAME}. "
            f"Is AZURE_SEARCH_KNOWLEDGE_BASE a knowledge base rather than a knowledge source?",
        }

    return {"ok": True, "server": server, "tools": tools}


def status(enabled=True):
    """What the console shows about the knowledge base, minus the key."""
    configured, active = is_configured(), is_configured() and bool(enabled)
    if active:
        summary = f"{knowledge_base_name()} via MCP ({api_version()})"
    elif configured:
        summary = f"{knowledge_base_name()} configured but switched off in settings"
    elif mcp_url():
        summary = "not active — AZURE_SEARCH_API_KEY is not set"
    else:
        summary = "not configured"

    return {
        "configured": configured,
        "enabled": bool(enabled),
        "active": active,
        "summary": summary,
        "name": knowledge_base_name(),
        "server_label": server_label(),
        "tool": TOOL_NAME,
        "url": mcp_url(),
        "api_version": api_version(),
        "has_key": bool(api_key()),
    }
