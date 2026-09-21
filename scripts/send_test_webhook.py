"""Post a correctly signed `realtime.call.incoming` event at the local server.

Exercises the webhook, customer lookup and accept path without placing a call.

Signing follows the Standard Webhooks scheme Azure OpenAI uses:
    base64(HMAC-SHA256(secret, "{webhook-id}.{timestamp}.{body}"))

    python scripts/send_test_webhook.py
    python scripts/send_test_webhook.py --url http://127.0.0.1:8000/webhook
    python scripts/send_test_webhook.py --from +97455512345
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import uuid

import requests
from dotenv import load_dotenv

load_dotenv()

SECRET = (os.environ.get("AZURE_OPENAI_WEBHOOK_SECRET") or "").strip()
if not SECRET:
    sys.exit("AZURE_OPENAI_WEBHOOK_SECRET must be set in .env")


def main():
    port = os.environ.get("PORT", "8000")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=f"http://127.0.0.1:{port}/webhook")
    parser.add_argument(
        "--from",
        dest="from_number",
        default="+97455512345",
        help="Caller number placed in the From SIP header",
    )
    parser.add_argument("--call-id", dest="call_id", default=None)
    args = parser.parse_args()

    call_id = args.call_id or f"rtc_test_{uuid.uuid4().hex}"
    payload = json.dumps(
        {
            "object": "event",
            "id": f"evt_{uuid.uuid4().hex}",
            "type": "realtime.call.incoming",
            "created_at": int(time.time()),
            "data": {
                "call_id": call_id,
                "sip_headers": [
                    {"name": "From", "value": f"sip:{args.from_number}@sip.example.com"},
                    {"name": "To", "value": "sip:+97444001122@sip.example.com"},
                    {"name": "Call-ID", "value": call_id},
                ],
            },
        }
    )

    webhook_id = f"wh_{uuid.uuid4().hex}"
    timestamp = str(int(time.time()))

    key = (
        base64.b64decode(SECRET[len("whsec_") :])
        if SECRET.startswith("whsec_")
        else SECRET.encode()
    )
    signature = base64.b64encode(
        hmac.new(
            key, f"{webhook_id}.{timestamp}.{payload}".encode(), hashlib.sha256
        ).digest()
    ).decode()

    print(f"POST {args.url}")
    print(f"  call_id: {call_id}")
    print(f"  from:    {args.from_number}")

    try:
        response = requests.post(
            args.url,
            data=payload.encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "webhook-id": webhook_id,
                "webhook-timestamp": timestamp,
                "webhook-signature": f"v1,{signature}",
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        sys.exit(
            f"  -> could not reach {args.url}\n     {exc}\n\n"
            "Start the server first with ./run.ps1 (or ./run.sh)."
        )

    body = response.text
    print(f"  -> {response.status_code} {response.reason} {body}")

    if response.status_code == 400:
        sys.exit(
            "\nFAIL: the signature was rejected. AZURE_OPENAI_WEBHOOK_SECRET does not match\n"
            "the value the running server loaded. Restart the server after editing .env."
        )

    upstream_status = None
    try:
        upstream_status = json.loads(body).get("status")
    except (json.JSONDecodeError, AttributeError):
        pass

    if upstream_status == 404 or "call_id_not_found" in body:
        print(
            "\nPASS: signature verified and the accept request reached Azure OpenAI.\n\n"
            "Azure replied call_id_not_found because this event carries a synthetic call_id\n"
            "with no live SIP session behind it. That is the expected result here, and it\n"
            "confirms the webhook secret, endpoint URL and API key are all correct.\n"
            "(A bad key would return 401, not 404.)\n\n"
            "Check the server log to see which customer was matched for this From number."
        )
        return

    if not response.ok:
        sys.exit("\nFAIL: see the server log for the response from Azure OpenAI.")

    print("\nPASS: the webhook was accepted end to end.")


if __name__ == "__main__":
    main()
