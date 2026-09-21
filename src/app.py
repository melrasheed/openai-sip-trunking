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

import requests
import websockets
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request
from openai import InvalidWebhookSignatureError, OpenAI

import context
import db
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

DEFAULT_INSTRUCTIONS = (
    "You are the virtual assistant for {bank}. You are speaking with a customer "
    "on the telephone."
)

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
    values["instructions"] = (
        stored.get("agent_instructions")
        or os.environ.get("AGENT_INSTRUCTIONS")
        or DEFAULT_INSTRUCTIONS.format(bank=values["bank_name_en"])
    )
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


def build_accept_body(customer):
    """The accept payload, per the guide, with the caller's context folded in.

    Everything the agent needs lives here, including how to open the call. The
    greeting is deliberately *not* sent as a per-response override: instructions
    on `response.create` replace the session instructions for that response, and
    that stripped the dialect and profile from the agent's first sentence.
    """
    values = agent_settings()
    language = (customer or {}).get("preferred_language", "en")

    body = {
        "type": "realtime",
        "instructions": context.build_instructions(
            values["instructions"],
            customer,
            dialect_prompts(),
            bank_en=values["bank_name_en"],
            bank_ar=values["bank_name_ar"],
        ),
        # Azure expects the deployment name here, not the underlying model name.
        "model": DEPLOYMENT,
    }

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


def _handle_event(call_id, event):
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

    # Structural fallback: any other transcription-flavoured event that carries
    # text still gets recorded, whatever the service decided to call it.
    if "transcription" in event_type and (event.get("transcript") or "").strip():
        db.add_transcript_line(call_id, "caller", event["transcript"])
        logger.info(f"[{call_id}] caller transcript via {event_type}")


async def websocket_task(call_id):
    """Monitors the realtime session, opens the call, and logs the transcript."""
    url = WS_BASE + "/realtime?call_id=" + call_id
    try:
        async with websockets.connect(url, additional_headers=AUTH_HEADER) as websocket:
            logger.info(f"[{call_id}] websocket connected")
            # No `instructions` here on purpose: they would override the session
            # instructions for this response, stripping the dialect, profile and
            # guardrails from the agent's opening line. The accept payload
            # already tells it how to open.
            await websocket.send(json.dumps({"type": "response.create"}))

            while True:
                message = await websocket.recv()
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

                _handle_event(call_id, event)

    except websockets.exceptions.ConnectionClosed as exc:
        logger.info(f"[{call_id}] websocket closed: code={exc.code} reason={exc.reason}")
    except Exception as exc:
        logger.error(f"[{call_id}] websocket error: {exc}", exc_info=True)
    finally:
        for item_id in list(_delta_buffers.get(call_id, {})):
            _flush_delta(call_id, item_id)
        _delta_buffers.pop(call_id, None)
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
        f"instructions={len(accept_body['instructions'])} chars"
    )
    try:
        accepted = requests.post(
            API_BASE + "/realtime/calls/" + call_id + "/accept",
            headers={**AUTH_HEADER, "Content-Type": "application/json"},
            json=accept_body,
            timeout=10,
        )
    except requests.RequestException as exc:
        logger.error(f"[{call_id}] accept request failed: {exc}")
        db.end_call(call_id, status="failed")
        return jsonify({"error": "accept failed", "status": 502}), 502

    logger.info(f"[{call_id}] accept -> {accepted.status_code} {accepted.text[:300]}")
    if not accepted.ok:
        db.end_call(call_id, status="failed")
        return jsonify({"error": "accept failed", "status": accepted.status_code}), 502

    settings = agent_settings()

    threading.Thread(
        target=lambda: asyncio.run(websocket_task(call_id)),
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
    if request.method == "PUT":
        data = request.get_json(silent=True) or {}
        updates = {}

        if "instructions" in data:
            updates["agent_instructions"] = data.get("instructions") or ""
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
            "instructions": values["instructions"],
            "settings": settings_spec.describe(values),
            "prompts": dialect_prompts(),
            "deployment": DEPLOYMENT,
            "endpoint": ENDPOINT,
            "arabic_variants": context.ARABIC_VARIANTS,
        }
    )


@app.route("/api/preview", methods=["GET"])
def api_preview():
    """Shows the instructions a given number would produce, without a call."""
    number = request.args.get("number", "")
    customer = db.find_customer_by_number(number) if number else None
    values = agent_settings()
    language = (customer or {}).get("preferred_language", "en")
    return jsonify(
        {
            "number": number,
            "matched": bool(customer),
            "customer": customer["full_name_en"] if customer else None,
            "language": language,
            "arabic_variant": (customer or {}).get("arabic_variant", "default"),
            "instructions": build_accept_body(customer)["instructions"],
            "opening": context.opening_directive(
                customer, values["bank_name_en"], values["bank_name_ar"]
            ),
            "accept_body": build_accept_body(customer),
        }
    )


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
        f"speed={values['speed']}"
    )
    logger.info(
        f"Transcription: {values['transcription_model'] or 'disabled (assistant side only)'}"
    )
    if inserted:
        logger.info(f"Seeded {inserted} demo customers")
    logger.info(f"Console:       http://127.0.0.1:{port}/")

    app.run(host="0.0.0.0", port=port, threaded=True)
