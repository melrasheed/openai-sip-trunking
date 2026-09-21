"""Answers inbound SIP calls with the Azure OpenAI GPT Realtime API.

Python port of https://developers.openai.com/api/docs/guides/voice-sip?api=realtime
with the three differences Azure requires:

  1. Base URL   https://<resource>.openai.azure.com/openai/v1
                instead of https://api.openai.com/v1
  2. Auth       "api-key: <key>" instead of "Authorization: Bearer <key>"
  3. model      the name of your *deployment*, not the model name

Run it with:  python src/app.py
"""

from flask import Flask, request, Response
from openai import OpenAI, InvalidWebhookSignatureError
from dotenv import load_dotenv
import asyncio
import json
import logging
import os
import requests
import sys
import threading
import websockets

# Flask buffers stdout when it is not a TTY, which would hide the event log.
sys.stdout.reconfigure(line_buffering=True)

# The repository keeps configuration in .env, so load it before reading os.environ.
load_dotenv()

app = Flask(__name__)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

ENDPOINT = os.environ["AZURE_OPENAI_ENDPOINT"].rstrip("/")
API_KEY = os.environ["AZURE_OPENAI_API_KEY"]
DEPLOYMENT = os.environ["AZURE_OPENAI_DEPLOYMENT"]
WEBHOOK_SECRET = os.environ["AZURE_OPENAI_WEBHOOK_SECRET"]

API_BASE = ENDPOINT + "/openai/v1"
WS_BASE = API_BASE.replace("https://", "wss://", 1)

# api_key is required by the constructor but never sent anywhere: this client
# is only used to verify webhook signatures.
client = OpenAI(
    api_key="unused-signature-verification-only",
    webhook_secret=WEBHOOK_SECRET,
)

AUTH_HEADER = {"api-key": API_KEY}

call_accept = {
    "type": "realtime",
    "instructions": os.environ.get("AGENT_INSTRUCTIONS", "You are a support agent."),
    # Azure expects the deployment name here, not the underlying model name.
    "model": DEPLOYMENT,
}

if os.environ.get("AGENT_VOICE", "").strip():
    call_accept["audio"] = {"output": {"voice": os.environ["AGENT_VOICE"].strip()}}



response_create = {
    "type": "response.create",
    "response": {
        "instructions": "Say to the user '{}'".format(
            os.environ.get(
                "WELCOME_GREETING", "Thank you for calling, how can I help you"
            )
        )
    },
}


async def websocket_task(call_id):
    """Monitors the realtime session and greets the caller."""
    try:
        async with websockets.connect(
            WS_BASE + "/realtime?call_id=" + call_id,
            additional_headers=AUTH_HEADER,
        ) as websocket:
            logger.info(f"[{call_id}] WebSocket connected")
            await websocket.send(json.dumps(response_create))
            while True:
                response = await websocket.recv()
                logger.info(f"[{call_id}] WebSocket event: {response}")
    except websockets.exceptions.ConnectionClosed as e:
        logger.info(f"[{call_id}] WebSocket closed: code={e.code} reason={e.reason}")
    except Exception as e:
        logger.error(f"[{call_id}] WebSocket error: {e}", exc_info=True)


@app.route("/", methods=["POST"])
@app.route("/webhook", methods=["POST"])
def webhook():
    """Webhook endpoint to receive and process Azure OpenAI events."""
    try:
        event = client.webhooks.unwrap(request.data, request.headers)

        if event.type == "realtime.call.incoming":
            call_id = event.data.call_id
            logger.info(f"[{call_id}] incoming call")

            accept_url = API_BASE + "/realtime/calls/" + call_id + "/accept"
            try:
                accepted = requests.post(
                    accept_url,
                    headers={**AUTH_HEADER, "Content-Type": "application/json"},
                    json=call_accept,
                    timeout=10,
                )
            except requests.RequestException as e:
                logger.error(f"[{call_id}] accept request failed: {e}", exc_info=True)
                return Response(
                    json.dumps({"error": "accept failed", "status": 502}),
                    status=502,
                    mimetype="application/json",
                )

            logger.info(f"[{call_id}] accept -> {accepted.status_code} {accepted.text}")
            if not accepted.ok:
                # Same shape as the TypeScript server, so scripts/send-test-webhook.ts
                # can interpret the failure against either implementation.
                return Response(
                    json.dumps({"error": "accept failed", "status": accepted.status_code}),
                    status=502,
                    mimetype="application/json",
                )

            threading.Thread(
                target=lambda: asyncio.run(websocket_task(call_id)),
                name=f"websocket-{call_id}",
                daemon=True,
            ).start()
            logger.info(f"------------Accepted Call with Id {call_id}------------)")
        else:
            logger.info(f"Ignoring event of type {event.type}")

        return Response(status=200)
    except InvalidWebhookSignatureError as e:
        logger.error(f"Invalid signature: {e}")
        return Response("Invalid signature", status=400)
    except Exception as e:
        logger.error(f"Error processing webhook: {e}", exc_info=True)
        return Response("Internal server error", status=500)


@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok", "deployment": DEPLOYMENT}


if __name__ == "__main__":
    logger.info(f"Endpoint:    {ENDPOINT}")
    logger.info(f"Deployment:  {DEPLOYMENT}")
    logger.info(f"Accept body: {json.dumps(call_accept)}")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))