"""Contextualised banking voice agent on Azure OpenAI GPT Realtime, over SIP.

Based on https://developers.openai.com/api/docs/guides/voice-sip?api=realtime
with the three differences Azure requires:

  1. Base URL   https://<resource>.openai.azure.com/openai/v1
                instead of https://api.openai.com/v1
  2. Auth       "api-key: <key>" instead of "Authorization: Bearer <key>"
  3. model      the name of your *deployment*, not the model name

On top of the reference flow, an inbound call is contextualised before it is
answered: the caller's number is read from the SIP `From` header carried on the
webhook, matched against the customer database, and the resulting profile is
injected into the accept-time `instructions`. The agent therefore knows who it
is speaking to from its first word.

Serves the demo web UI on the same port, so a single tunnel exposes both.

Run it with:  ./run.ps1   (or ./run.sh, or python src/app.py)
"""

import asyncio
import json
import logging
import os
import sys
import threading
import time

import requests
import websockets
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request
from openai import InvalidWebhookSignatureError, OpenAI

import context
import db
import knowledge
import settings_spec

# Flask buffers stdout when it is not a TTY, which would hide the event log.
sys.stdout.reconfigure(line_buffering=True)

load_dotenv()

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("agent")

ENDPOINT = os.environ["AZURE_OPENAI_ENDPOINT"].rstrip("/")
API_KEY = os.environ["AZURE_OPENAI_API_KEY"]
DEPLOYMENT = os.environ["AZURE_OPENAI_DEPLOYMENT"]
WEBHOOK_SECRET = os.environ["AZURE_OPENAI_WEBHOOK_SECRET"]

# Optional. Seeds the transcription model on a fresh database; after that the
# console owns the value.
TRANSCRIPTION_MODEL_ENV = (os.environ.get("TRANSCRIPTION_MODEL") or "").strip()

API_BASE = ENDPOINT + "/openai/v1"
WS_BASE = API_BASE.replace("https://", "wss://", 1)
AUTH_HEADER = {"api-key": API_KEY}

# api_key is required by the constructor but never sent anywhere: this client
# only verifies webhook signatures.
verifier = OpenAI(
    api_key="unused-signature-verification-only",
    webhook_secret=WEBHOOK_SECRET,
)

app = Flask(__name__)
app.json.ensure_ascii = False  # keep Arabic readable in API responses
# Pick up template edits without a restart; this is a demo console, not a
# throughput-sensitive service.
app.config["TEMPLATES_AUTO_RELOAD"] = True


def agent_settings():
    """Runtime configuration: database overrides .env, .env overrides defaults."""
    stored = db.get_settings()
    values = settings_spec.resolve(stored)
    # The whole system prompt, placeholders and all. Seeded from
    # prompts/system_template.txt on first run and editable in the console; the
    # file is the fallback if the row was somehow emptied.
    values["template"] = stored.get(db.TEMPLATE_KEY) or db.default_template()
    # Environment can still seed the transcription model on a fresh database.
    if not stored.get("transcription_model") and TRANSCRIPTION_MODEL_ENV:
        values["transcription_model"] = TRANSCRIPTION_MODEL_ENV
    return values


def dialect_prompts():
    stored = db.get_settings()
    return {
        "faseeh": stored.get("prompt_faseeh", ""),
        "qatari": stored.get("prompt_qatari", ""),
    }


def knowledge_directive(values=None):
    """The prompt section naming the knowledge base, or "" when it is off."""
    values = values if values is not None else agent_settings()
    if not values.get("knowledge_enabled") or not knowledge.is_configured():
        return ""
    return knowledge.directive(values.get("knowledge_topic"))


def build_accept_body(customer):
    """The accept payload, per the guide, with the caller's context folded in.

    Everything the agent needs lives here, including how to open the call. The
    greeting is deliberately *not* sent as a per-response override: instructions
    on `response.create` replace the session instructions for that response, and
    that stripped the dialect and profile from the agent's first sentence.
    """
    values = agent_settings()
    # None rather than "en" when nobody matched, so the caller-transcription
    # hint is omitted instead of guessing a language for an unknown caller.
    language = (customer or {}).get("preferred_language")
    directive = knowledge_directive(values)

    body = {
        "type": "realtime",
        "instructions": context.build_instructions(
            values["template"],
            customer,
            dialect_prompts(),
            bank_en=values["bank_name_en"],
            bank_ar=values["bank_name_ar"],
            voice_style=values["voice_style"],
            knowledge_directive=directive,
        ),
        # Azure expects the deployment name here, not the underlying model name.
        "model": DEPLOYMENT,
    }

    if directive:
        # The knowledge base is a remote MCP server: the service calls it
        # directly, so nothing here has to proxy the search.
        body["tools"] = [knowledge.build_tool()]
        body["tool_choice"] = "auto"

    audio = settings_spec.build_audio(values, language)
    if audio:
        body["audio"] = audio

    # `temperature` and `max_response_output_tokens` are rejected by the GA
    # session shape; `max_output_tokens` is the supported spelling.
    max_tokens = values.get("max_output_tokens") or 0
    if max_tokens > 0:
        body["max_output_tokens"] = int(max_tokens)

    return body


# ---------------------------------------------------------------------------
# Realtime session monitor
# ---------------------------------------------------------------------------


"""Records the parts of the event stream the UI needs.

Azure documents two spellings for the caller-transcription events, and this
deployment may emit either, or stream deltas instead of one completed event.
Rather than betting on a name, handle every documented spelling and fall back
to matching structurally.
"""

TRANSCRIPT_DONE = {
    "conversation.item.input_audio_transcription.completed",
    "conversation.item.audio_transcription.completed",
}
TRANSCRIPT_FAILED = {
    "conversation.item.input_audio_transcription.failed",
    "conversation.item.audio_transcription.failed",
}
TRANSCRIPT_DELTA = {
    "conversation.item.input_audio_transcription.delta",
    "conversation.item.audio_transcription.delta",
}

# Partial caller transcripts, keyed by call then item, for deployments that
# stream deltas without a final completed event.
_delta_buffers = {}


def _flush_delta(call_id, item_id):
    buffered = _delta_buffers.get(call_id, {}).pop(item_id, "")
    if buffered.strip():
        db.add_transcript_line(call_id, "caller", buffered)
        return True
    return False


# ---------------------------------------------------------------------------
# Knowledge base lookups
# ---------------------------------------------------------------------------
#
# A remote MCP tool is run by the Realtime service, not by us, so there is no
# result for us to hand back the way a local function tool would. The guide is
# blunt about what that costs us:
#
#     "After the response is done and all of its MCP calls have finished, send
#      another response.create event to let the model use the results and
#      proceed with the conversation. The Realtime API doesn't create these
#      follow-up responses automatically."
#
# Without that follow-up the agent says it will check, the search runs, and
# the line goes quiet until the caller asks again. That is the bug.
#
# Two details shape the code below. First, response.done can arrive before the
# search finishes, so this cannot be a sequence; it is a small state machine
# that fires when both halves are true, in whichever order they land. Second,
# a search can take ten to fifteen seconds, which is a long time to hold a
# phone to your ear in silence, so the same state also schedules short holding
# phrases while we wait.

_MCP_SETTLED = ("completed", "failed", "incomplete")

# How often the read loop wakes up with nothing to do. Holding phrases and the
# search watchdog are driven by the clock, not by events, so they need a tick
# even when the service has gone quiet.
_TICK_SECONDS = 1.0

_mcp_state = {}


def _mcp(call_id):
    """Per-call knowledge base state, created on first use."""
    return _mcp_state.setdefault(
        call_id,
        {
            "pending": set(),  # searches still running, by item id
            "used": False,  # the response being generated called a tool
            "armed": False,  # a finished response owes us a follow-up
            "active": False,  # the agent is speaking
            "caller": False,  # the caller is speaking
            "started": None,  # when the current wait began
            "next_hold": None,  # earliest the next holding phrase may go
            "holds": 0,
            "customer": None,
            "prompts": {},
            "hold_enabled": True,
            "hold_delay": 5.0,
            "hold_interval": 5.0,
            "hold_max": 2,
            "timeout": 30.0,
        },
    )


def _mcp_init(call_id, customer=None):
    """Loads the settings a call will need, once, at connect time."""
    values = agent_settings()
    state = _mcp(call_id)
    state["customer"] = customer
    state["prompts"] = dialect_prompts()
    state["hold_enabled"] = bool(values.get("knowledge_hold_enabled", True))
    state["hold_delay"] = max(0, int(values.get("knowledge_hold_delay_ms", 5000))) / 1000.0
    state["hold_interval"] = max(1, int(values.get("knowledge_hold_interval_ms", 5000))) / 1000.0
    state["hold_max"] = max(0, int(values.get("knowledge_hold_max", 2)))
    state["timeout"] = max(1, int(values.get("knowledge_timeout_ms", 30000))) / 1000.0
    return state


def _mcp_begin(state, item_id):
    """A search has started."""
    if not item_id or item_id in state["pending"]:
        return
    first = not state["pending"]
    state["pending"].add(item_id)
    # Only a real tool call earns a follow-up. Holding phrases and follow-ups
    # never set this, which is what stops them feeding each other.
    state["used"] = True
    if first:
        state["started"] = time.monotonic()
        state["holds"] = 0
        state["next_hold"] = state["started"] + state["hold_delay"]


def _mcp_end(state, item_id):
    """A search has finished, one way or another."""
    state["pending"].discard(item_id)
    if not state["pending"]:
        state["started"] = None
        state["next_hold"] = None
        state["holds"] = 0


def _mcp_track(call_id, event):
    """Folds one server event into the call's knowledge base state."""
    state = _mcp(call_id)
    event_type = event.get("type") or ""

    if event_type == "response.created":
        state["active"] = True
        return

    if event_type == "response.done":
        state["active"] = False
        if state["used"]:
            state["used"] = False
            state["armed"] = True
        return

    if event_type == "input_audio_buffer.speech_started":
        state["caller"] = True
        return

    if event_type in ("input_audio_buffer.speech_stopped", "input_audio_buffer.committed"):
        state["caller"] = False
        return

    if event_type == "response.mcp_call.in_progress" or event_type.startswith(
        "response.mcp_call_arguments."
    ):
        _mcp_begin(state, event.get("item_id"))
        return

    if event_type.startswith("response.mcp_call."):
        if event_type.rsplit(".", 1)[-1] in _MCP_SETTLED:
            _mcp_end(state, event.get("item_id"))
        return

    # Some deployments only report the finished tool call as an output item.
    if event_type == "response.output_item.done":
        item = event.get("item") or {}
        if item.get("type") == "mcp_call":
            _mcp_end(state, item.get("id"))


def _mcp_tick(call_id):
    """Returns whatever the call is owed right now: a follow-up, or a hold."""
    state = _mcp_state.get(call_id)
    if not state:
        return []

    now = time.monotonic()
    outbound = []

    # A search that never reports back would hold the caller for ever. Give up
    # on it and let the agent answer with what it already knows.
    if state["pending"] and state["started"] is not None:
        if now - state["started"] >= state["timeout"]:
            logger.error(f"[{call_id}] knowledge base search timed out, answering without it")
            db.add_transcript_line(call_id, "system", "Knowledge base search timed out")
            state["pending"].clear()
            state["used"] = False
            state["armed"] = True
            _mcp_end(state, None)

    # Never talk over either party.
    if state["active"] or state["caller"]:
        return outbound

    if state["armed"] and not state["pending"]:
        state["armed"] = False
        logger.info(f"[{call_id}] knowledge base ready, asking for the answer")
        db.add_transcript_line(call_id, "system", "Delivering knowledge base answer")
        # Deliberately bare: per-response instructions replace the session
        # instructions wholesale, which would strip the caller's language,
        # dialect and profile out of the one reply that most needs them.
        outbound.append({"type": "response.create"})
        return outbound

    if (
        state["pending"]
        and state["hold_enabled"]
        and state["next_hold"] is not None
        and state["holds"] < state["hold_max"]
        and now >= state["next_hold"]
    ):
        state["holds"] += 1
        state["next_hold"] = now + state["hold_interval"]
        logger.info(f"[{call_id}] search still running, holding phrase {state['holds']}")
        db.add_transcript_line(
            call_id, "system", f"Holding phrase {state['holds']} while the search runs"
        )
        outbound.append(
            {
                "type": "response.create",
                "response": {
                    "instructions": context.build_hold_instruction(
                        state["customer"], state["prompts"], state["holds"]
                    )
                },
            }
        )

    return outbound


def _handle_event(call_id, event):
    """Reacts to one server event and returns anything we owe the service back.

    Returns a list of outbound events rather than sending them, because this
    runs synchronously and has no socket of its own. The caller owns the
    socket and does the sending, which also makes the knowledge base logic
    testable without a live call.
    """
    _mcp_track(call_id, event)
    _record_event(call_id, event)
    return _mcp_tick(call_id)


def _record_event(call_id, event):
    event_type = event.get("type") or ""

    if event_type == "response.output_audio_transcript.done":
        db.add_transcript_line(call_id, "assistant", event.get("transcript", ""))
        return

    if event_type in TRANSCRIPT_DONE:
        text = event.get("transcript") or ""
        item_id = event.get("item_id")
        _delta_buffers.get(call_id, {}).pop(item_id, None)
        if text.strip():
            db.add_transcript_line(call_id, "caller", text)
        logger.info(f"[{call_id}] caller transcript: {text[:120]!r}")
        return

    if event_type in TRANSCRIPT_FAILED:
        error = event.get("error") or {}
        detail = error.get("message") or json.dumps(event)[:300]
        logger.error(f"[{call_id}] caller transcription FAILED: {detail}")
        db.add_transcript_line(
            call_id, "system", f"Caller transcription failed: {detail}"
        )
        return

    if event_type in TRANSCRIPT_DELTA:
        item_id = event.get("item_id")
        chunk = event.get("delta") or event.get("transcript") or ""
        buffers = _delta_buffers.setdefault(call_id, {})
        buffers[item_id] = buffers.get(item_id, "") + chunk
        return

    if event_type == "conversation.item.done" or event_type == "conversation.item.created":
        _flush_delta(call_id, (event.get("item") or {}).get("id"))
        return

    if event_type == "error":
        db.add_transcript_line(call_id, "system", f"error: {json.dumps(event)[:500]}")
        logger.error(f"[{call_id}] realtime error: {json.dumps(event)[:500]}")
        return

    # Knowledge base lookups. The service talks to the MCP server itself, so
    # these events are the only window onto whether a search happened.
    if event_type.startswith("mcp_list_tools."):
        stage = event_type.split(".", 1)[1]
        if stage == "failed":
            detail = (event.get("error") or {}).get("message") or json.dumps(event)[:300]
            logger.error(f"[{call_id}] knowledge base unreachable: {detail}")
            db.add_transcript_line(
                call_id, "system", f"Knowledge base unavailable: {detail}"
            )
        elif stage == "completed":
            logger.info(f"[{call_id}] knowledge base connected")
        return

    if event_type == "response.mcp_call_arguments.done":
        query = event.get("arguments") or ""
        logger.info(f"[{call_id}] knowledge base query: {query[:200]}")
        db.add_transcript_line(call_id, "system", f"Knowledge base query: {query[:300]}")
        return

    if event_type.startswith("response.mcp_call."):
        stage = event_type.split(".")[-1]
        if stage == "failed":
            detail = (event.get("error") or {}).get("message") or json.dumps(event)[:300]
            logger.error(f"[{call_id}] knowledge base search FAILED: {detail}")
            db.add_transcript_line(
                call_id, "system", f"Knowledge base search failed: {detail}"
            )
        elif stage == "completed":
            logger.info(f"[{call_id}] knowledge base search completed")
        return

    # Structural fallback: any other transcription-flavoured event that carries
    # text still gets recorded, whatever the service decided to call it.
    if "transcription" in event_type and (event.get("transcript") or "").strip():
        db.add_transcript_line(call_id, "caller", event["transcript"])
        logger.info(f"[{call_id}] caller transcript via {event_type}")


async def websocket_task(call_id, customer=None):
    """Monitors the realtime session, opens the call, and logs the transcript."""
    url = WS_BASE + "/realtime?call_id=" + call_id
    try:
        async with websockets.connect(url, additional_headers=AUTH_HEADER) as websocket:
            logger.info(f"[{call_id}] websocket connected")
            _mcp_init(call_id, customer)

            async def send(events):
                for outbound in events:
                    await websocket.send(json.dumps(outbound))

            # No `instructions` here on purpose: they would override the session
            # instructions for this response, stripping the dialect, profile and
            # guardrails from the agent's opening line. The accept payload
            # already tells it how to open.
            await websocket.send(json.dumps({"type": "response.create"}))

            while True:
                try:
                    message = await asyncio.wait_for(websocket.recv(), timeout=_TICK_SECONDS)
                except asyncio.TimeoutError:
                    # A quiet socket still needs servicing: holding phrases and
                    # the search watchdog run off the clock, not off events.
                    await send(_mcp_tick(call_id))
                    continue

                try:
                    event = json.loads(message)
                except json.JSONDecodeError:
                    continue

                event_type = event.get("type") or ""
                # Transcription events are logged in full: they are the ones
                # worth diagnosing, and they are rare enough not to flood.
                if "transcription" in event_type:
                    logger.info(f"[{call_id}] <- {json.dumps(event)[:600]}")
                elif event_type == "error" or not event_type.endswith(".delta"):
                    logger.info(f"[{call_id}] <- {event_type}")

                await send(_handle_event(call_id, event))

    except websockets.exceptions.ConnectionClosed as exc:
        logger.info(f"[{call_id}] websocket closed: code={exc.code} reason={exc.reason}")
    except Exception as exc:
        logger.error(f"[{call_id}] websocket error: {exc}", exc_info=True)
    finally:
        for item_id in list(_delta_buffers.get(call_id, {})):
            _flush_delta(call_id, item_id)
        _delta_buffers.pop(call_id, None)
        _mcp_state.pop(call_id, None)
        db.end_call(call_id)


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------


def _sip_headers_from_request(raw_body):
    """Reads sip_headers straight off the raw payload.

    The SDK's typed webhook model does not expose `sip_headers`, so it is read
    from the raw JSON. The signature has already been verified by unwrap() at
    this point, so the payload is trusted.
    """
    try:
        return json.loads(raw_body).get("data", {}).get("sip_headers", [])
    except (json.JSONDecodeError, AttributeError):
        return []


@app.route("/webhook", methods=["POST"])
@app.route("/api/webhook", methods=["POST"])
def webhook():
    raw_body = request.data
    try:
        event = verifier.webhooks.unwrap(raw_body, request.headers)
    except InvalidWebhookSignatureError as exc:
        logger.error(f"invalid signature: {exc}")
        return Response("Invalid signature", status=400)
    except Exception as exc:
        logger.error(f"could not parse webhook: {exc}", exc_info=True)
        return Response("Internal server error", status=500)

    if event.type != "realtime.call.incoming":
        logger.info(f"ignoring event of type {event.type}")
        return Response(status=200)

    call_id = event.data.call_id
    sip_headers = _sip_headers_from_request(raw_body)
    from_number = context.caller_number(sip_headers)

    customer = db.find_customer_by_number(from_number) if from_number else None
    language = (customer or {}).get("preferred_language", "en")

    if customer:
        variant = customer.get("arabic_variant") or "default"
        style = f" style={variant}" if language == "ar" and variant != "default" else ""
        logger.info(
            f"[{call_id}] incoming from {from_number} -> "
            f"{customer['full_name_en']} (language={language}{style})"
        )
    else:
        logger.info(f"[{call_id}] incoming from {from_number or 'unknown'} -> no match")

    db.start_call(call_id, from_number, (customer or {}).get("id"), language)

    accept_body = build_accept_body(customer)
    # Logged so a misconfigured setting is diagnosable. Instructions are long,
    # so only their size is recorded here.
    logger.info(
        f"[{call_id}] accept config: audio={json.dumps(accept_body.get('audio', {}))} "
        f"instructions={len(accept_body['instructions'])} chars "
        f"knowledge={'on' if accept_body.get('tools') else 'off'}"
    )

    def post_accept(body):
        return requests.post(
            API_BASE + "/realtime/calls/" + call_id + "/accept",
            headers={**AUTH_HEADER, "Content-Type": "application/json"},
            json=body,
            timeout=10,
        )

    try:
        accepted = post_accept(accept_body)
        if not accepted.ok and accept_body.get("tools"):
            # Never drop a call over the knowledge base. If this deployment
            # rejects the MCP tool, answer without it and say so in the log.
            logger.error(
                f"[{call_id}] accept rejected with knowledge base "
                f"({accepted.status_code} {accepted.text[:300]}); retrying without it"
            )
            retry = {k: v for k, v in accept_body.items() if k not in ("tools", "tool_choice")}
            accepted = post_accept(retry)
            if accepted.ok:
                db.add_transcript_line(
                    call_id,
                    "system",
                    "Knowledge base rejected by the realtime service; "
                    "the call continues without it.",
                )
    except requests.RequestException as exc:
        logger.error(f"[{call_id}] accept request failed: {exc}")
        db.end_call(call_id, status="failed")
        return jsonify({"error": "accept failed", "status": 502}), 502

    logger.info(f"[{call_id}] accept -> {accepted.status_code} {accepted.text[:300]}")
    if not accepted.ok:
        db.end_call(call_id, status="failed")
        return jsonify({"error": "accept failed", "status": accepted.status_code}), 502

    threading.Thread(
        target=lambda: asyncio.run(websocket_task(call_id, customer)),
        name=f"ws-{call_id}",
        daemon=True,
    ).start()

    return Response(status=200)


# ---------------------------------------------------------------------------
# JSON API for the UI
# ---------------------------------------------------------------------------


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "deployment": DEPLOYMENT})


@app.route("/api/customers", methods=["GET", "POST"])
def api_customers():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        if not (data.get("full_name_en") or "").strip():
            return jsonify({"error": "full_name_en is required"}), 400
        if not (data.get("mobile_e164") or "").strip():
            return jsonify({"error": "mobile_e164 is required"}), 400
        return jsonify({"id": db.create_customer(data)}), 201
    return jsonify(db.list_customers())


@app.route("/api/customers/<int:customer_id>", methods=["GET", "PUT", "DELETE"])
def api_customer(customer_id):
    if request.method == "PUT":
        data = request.get_json(silent=True) or {}
        if not db.update_customer(customer_id, data):
            return jsonify({"error": "not found or nothing to update"}), 404
        return jsonify(db.get_customer(customer_id))

    if request.method == "DELETE":
        if not db.delete_customer(customer_id):
            return jsonify({"error": "not found"}), 404
        return jsonify({"deleted": customer_id})

    customer = db.get_customer(customer_id)
    return jsonify(customer) if customer else (jsonify({"error": "not found"}), 404)


@app.route("/api/calls", methods=["GET"])
def api_calls():
    return jsonify(db.list_calls())


@app.route("/api/calls/<call_id>/transcript", methods=["GET"])
def api_transcript(call_id):
    after = request.args.get("after", default=0, type=int)
    return jsonify(db.get_transcript(call_id, after_id=after))


@app.route("/api/settings", methods=["GET", "PUT"])
def api_settings():
    warnings = []

    if request.method == "PUT":
        data = request.get_json(silent=True) or {}
        updates = {}

        if "template" in data:
            template = data.get("template") or ""
            unknown = context.unknown_placeholders(template)
            if unknown:
                # Rejected rather than silently left in place: a misspelt
                # placeholder would be read out to the caller verbatim.
                return (
                    jsonify(
                        {
                            "error": "unknown placeholders: "
                            + ", ".join("{" + name + "}" for name in unknown),
                            "unknown": unknown,
                            "allowed": list(context.PLACEHOLDERS),
                        }
                    ),
                    400,
                )
            # Dropping one of these is a legitimate (if odd) choice, so it is a
            # warning rather than a refusal.
            warnings = [
                "{" + name + "} is missing — the agent will lose that section."
                for name in context.missing_placeholders(template)
            ]
            updates[db.TEMPLATE_KEY] = template

        for variant, key in (("faseeh", "prompt_faseeh"), ("qatari", "prompt_qatari")):
            if variant in (data.get("prompts") or {}):
                updates[key] = data["prompts"][variant] or ""
        for name in settings_spec.SPEC:
            if name in data:
                value = data[name]
                updates[name] = "" if value is None else str(value)

        if updates:
            db.set_settings(updates)

    values = agent_settings()
    return jsonify(
        {
            "template": values["template"],
            "default_template": db.default_template(),
            "placeholders": context.PLACEHOLDERS,
            "required_placeholders": list(context.REQUIRED_PLACEHOLDERS),
            "warnings": warnings,
            "settings": settings_spec.describe(values),
            "prompts": dialect_prompts(),
            "deployment": DEPLOYMENT,
            "endpoint": ENDPOINT,
            "arabic_variants": context.ARABIC_VARIANTS,
            "knowledge": knowledge.status(bool(values.get("knowledge_enabled"))),
        }
    )


@app.route("/api/settings/reset-template", methods=["POST"])
def api_reset_template():
    """Puts the shipped prompt template back, discarding console edits."""
    return jsonify({"template": db.reset_template()})


@app.route("/api/preview", methods=["GET"])
def api_preview():
    """Shows the instructions a given number would produce, without a call."""
    number = request.args.get("number", "")
    customer = db.find_customer_by_number(number) if number else None
    values = agent_settings()
    language = (customer or {}).get("preferred_language", "en")
    accept_body = build_accept_body(customer)
    return jsonify(
        {
            "number": number,
            "matched": bool(customer),
            "customer": customer["full_name_en"] if customer else None,
            "language": language,
            "arabic_variant": (customer or {}).get("arabic_variant", "default"),
            "voice_style": values["voice_style"],
            "instructions": accept_body["instructions"],
            "opening": context.opening_directive(
                customer, values["bank_name_en"], values["bank_name_ar"]
            ),
            # Redacted: the payload carries the search key in the MCP headers.
            "accept_body": knowledge.redact(accept_body),
        }
    )


@app.route("/api/knowledge", methods=["GET"])
def api_knowledge():
    """Whether the knowledge base is wired up, without contacting it."""
    values = agent_settings()
    return jsonify(knowledge.status(bool(values.get("knowledge_enabled"))))


@app.route("/api/knowledge/test", methods=["POST"])
def api_knowledge_test():
    """Calls the knowledge base's MCP endpoint and reports what came back."""
    if not knowledge.is_configured():
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "No knowledge base configured. Set AZURE_SEARCH_ENDPOINT, "
                    "AZURE_SEARCH_KNOWLEDGE_BASE and AZURE_SEARCH_API_KEY.",
                }
            ),
            400,
        )
    result = knowledge.probe()
    return jsonify(result), (200 if result.get("ok") else 502)


@app.route("/api/stats", methods=["GET"])
def api_stats():
    return jsonify(db.stats())


@app.route("/api/seed", methods=["POST"])
def api_seed():
    return jsonify({"inserted": db.seed(force=True)})


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")


if __name__ == "__main__":
    db.init_db()
    inserted = db.seed()

    values = agent_settings()
    port = int(os.environ.get("PORT", 8000))
    logger.info(f"Endpoint:      {ENDPOINT}")
    logger.info(f"Deployment:    {DEPLOYMENT}")
    logger.info(f"Database:      {db.DB_PATH}")
    logger.info(
        f"Voices:        en={values['voice_en']} ar={values['voice_ar']} "
        f"speed={values['speed']} style={values['voice_style']}"
    )
    logger.info(
        f"Transcription: {values['transcription_model'] or 'disabled (assistant side only)'}"
    )
    kb = knowledge.status(bool(values.get("knowledge_enabled")))
    logger.info(f"Knowledge:     {kb['summary']}")
    if inserted:
        logger.info(f"Seeded {inserted} demo customers")
    logger.info(f"Console:       http://127.0.0.1:{port}/")

    app.run(host="0.0.0.0", port=port, threaded=True)
