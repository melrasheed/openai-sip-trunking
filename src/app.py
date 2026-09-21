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

# Optional. Azure requires a *deployment* name here, not a model name, so
# caller-side transcription stays opt-in rather than breaking a working accept.
TRANSCRIBE_DEPLOYMENT = (os.environ.get("AZURE_OPENAI_TRANSCRIBE_DEPLOYMENT") or "").strip()

API_BASE = ENDPOINT + "/openai/v1"
WS_BASE = API_BASE.replace("https://", "wss://", 1)
AUTH_HEADER = {"api-key": API_KEY}

DEFAULT_INSTRUCTIONS = (
    "You are the virtual assistant for a retail bank. You are speaking with a "
    "customer on the telephone."
)
DEFAULT_GREETING = "Thank you for calling, how can I help you?"

# api_key is required by the constructor but never sent anywhere: this client
# only verifies webhook signatures.
verifier = OpenAI(
    api_key="unused-signature-verification-only",
    webhook_secret=WEBHOOK_SECRET,
)

app = Flask(__name__)
app.json.ensure_ascii = False  # keep Arabic readable in API responses


def agent_settings():
    """Runtime agent configuration: database overrides .env, .env overrides defaults."""
    stored = db.get_settings()
    return {
        "instructions": stored.get("agent_instructions")
        or os.environ.get("AGENT_INSTRUCTIONS")
        or DEFAULT_INSTRUCTIONS,
        "greeting": stored.get("welcome_greeting")
        or os.environ.get("WELCOME_GREETING")
        or DEFAULT_GREETING,
        "voice": stored.get("agent_voice") or os.environ.get("AGENT_VOICE", "").strip(),
    }


def build_accept_body(customer):
    """The accept payload, per the guide, with the caller's profile folded in."""
    settings = agent_settings()

    body = {
        "type": "realtime",
        "instructions": context.build_instructions(settings["instructions"], customer),
        # Azure expects the deployment name here, not the underlying model name.
        "model": DEPLOYMENT,
    }

    audio = {}
    if settings["voice"]:
        audio["output"] = {"voice": settings["voice"]}
    if TRANSCRIBE_DEPLOYMENT:
        audio["input"] = {"transcription": {"model": TRANSCRIBE_DEPLOYMENT}}
    if audio:
        body["audio"] = audio

    return body


# ---------------------------------------------------------------------------
# Realtime session monitor
# ---------------------------------------------------------------------------


def _handle_event(call_id, event):
    """Records the parts of the event stream the UI needs."""
    event_type = event.get("type")

    if event_type == "response.output_audio_transcript.done":
        db.add_transcript_line(call_id, "assistant", event.get("transcript", ""))
    elif event_type == "conversation.item.input_audio_transcription.completed":
        db.add_transcript_line(call_id, "caller", event.get("transcript", ""))
    elif event_type == "error":
        db.add_transcript_line(call_id, "system", f"error: {json.dumps(event)[:500]}")
        logger.error(f"[{call_id}] realtime error: {json.dumps(event)[:500]}")


async def websocket_task(call_id, greeting_instruction):
    """Monitors the realtime session, greets the caller, and logs the transcript."""
    url = WS_BASE + "/realtime?call_id=" + call_id
    try:
        async with websockets.connect(url, additional_headers=AUTH_HEADER) as websocket:
            logger.info(f"[{call_id}] websocket connected")
            await websocket.send(
                json.dumps(
                    {
                        "type": "response.create",
                        "response": {"instructions": greeting_instruction},
                    }
                )
            )

            while True:
                message = await websocket.recv()
                try:
                    event = json.loads(message)
                except json.JSONDecodeError:
                    continue

                event_type = event.get("type")
                if event_type == "error" or not event_type.endswith(".delta"):
                    logger.info(f"[{call_id}] <- {event_type}")
                _handle_event(call_id, event)

    except websockets.exceptions.ConnectionClosed as exc:
        logger.info(f"[{call_id}] websocket closed: code={exc.code} reason={exc.reason}")
    except Exception as exc:
        logger.error(f"[{call_id}] websocket error: {exc}", exc_info=True)
    finally:
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
        logger.info(
            f"[{call_id}] incoming from {from_number} -> "
            f"{customer['full_name_en']} (language={language})"
        )
    else:
        logger.info(f"[{call_id}] incoming from {from_number or 'unknown'} -> no match")

    db.start_call(call_id, from_number, (customer or {}).get("id"), language)

    accept_body = build_accept_body(customer)
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
    greeting = context.build_greeting(customer, settings["greeting"])
    db.add_transcript_line(
        call_id,
        "system",
        f"Matched {customer['full_name_en']}" if customer else "No customer match",
    )

    threading.Thread(
        target=lambda: asyncio.run(websocket_task(call_id, greeting)),
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
        db.set_settings(
            {
                key: (data.get(field) or "")
                for key, field in (
                    ("agent_instructions", "instructions"),
                    ("welcome_greeting", "greeting"),
                    ("agent_voice", "voice"),
                )
                if field in data
            }
        )
    settings = agent_settings()
    settings["deployment"] = DEPLOYMENT
    settings["endpoint"] = ENDPOINT
    settings["transcription"] = TRANSCRIBE_DEPLOYMENT or None
    return jsonify(settings)


@app.route("/api/preview", methods=["GET"])
def api_preview():
    """Shows the instructions a given number would produce, without a call."""
    number = request.args.get("number", "")
    customer = db.find_customer_by_number(number) if number else None
    settings = agent_settings()
    return jsonify(
        {
            "number": number,
            "matched": bool(customer),
            "customer": customer["full_name_en"] if customer else None,
            "instructions": context.build_instructions(settings["instructions"], customer),
            "greeting": context.build_greeting(customer, settings["greeting"]),
        }
    )


@app.route("/api/seed", methods=["POST"])
def api_seed():
    return jsonify({"inserted": db.seed(force=True)})


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")


if __name__ == "__main__":
    db.init_db()
    inserted = db.seed()

    port = int(os.environ.get("PORT", 8000))
    logger.info(f"Endpoint:     {ENDPOINT}")
    logger.info(f"Deployment:   {DEPLOYMENT}")
    logger.info(f"Database:     {db.DB_PATH}")
    logger.info(f"Transcription:{' ' + TRANSCRIBE_DEPLOYMENT if TRANSCRIBE_DEPLOYMENT else ' disabled (assistant side only)'}")
    if inserted:
        logger.info(f"Seeded {inserted} demo customers")
    logger.info(f"UI:           http://127.0.0.1:{port}/")

    app.run(host="0.0.0.0", port=port, threaded=True)
