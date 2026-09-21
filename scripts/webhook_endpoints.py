"""Manage Azure OpenAI webhook endpoints.

Azure has no portal UI for these, so registration is REST-only:
    POST/GET/DELETE {endpoint}/openai/v1/dashboard/webhook_endpoints

    python scripts/webhook_endpoints.py create        register WEBHOOK_PUBLIC_URL
    python scripts/webhook_endpoints.py list          list registered endpoints
    python scripts/webhook_endpoints.py delete <id>   delete one
"""

import json
import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

ENDPOINT = (os.environ.get("AZURE_OPENAI_ENDPOINT") or "").strip().rstrip("/")
API_KEY = (os.environ.get("AZURE_OPENAI_API_KEY") or "").strip()

if not ENDPOINT or not API_KEY:
    sys.exit("AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY must be set in .env")

URL_BASE = ENDPOINT + "/openai/v1/dashboard/webhook_endpoints"
EVENT_TYPES = ["realtime.call.incoming"]


def request(method, url, body=None):
    headers = {"api-key": API_KEY, "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"

    try:
        response = requests.request(method, url, headers=headers, json=body, timeout=30)
    except requests.RequestException as exc:
        sys.exit(
            f"Could not reach {url}\n  {exc}\n\n"
            "Check that AZURE_OPENAI_ENDPOINT points at your Azure OpenAI resource."
        )

    if not response.ok:
        sys.exit(f"{method} {url} -> {response.status_code}\n{response.text}")

    return response.json() if response.text else None


def create():
    url = (os.environ.get("WEBHOOK_PUBLIC_URL") or "").strip()
    if not url.startswith("https://"):
        sys.exit(
            "WEBHOOK_PUBLIC_URL must be set to this server's public https webhook route,\n"
            "for example https://<tunnel-id>-8000.<cluster>.devtunnels.ms/webhook"
        )

    result = request(
        "POST",
        URL_BASE,
        {
            "name": (os.environ.get("WEBHOOK_NAME") or "sip-realtime-listener").strip(),
            "url": url,
            "event_types": EVENT_TYPES,
        },
    )

    print(json.dumps(result, indent=2))
    print()
    print("-" * 72)
    print("Copy signing_secret above into .env as AZURE_OPENAI_WEBHOOK_SECRET.")
    print("It is shown only once and cannot be retrieved again.")
    print("-" * 72)


def main():
    args = sys.argv[1:]
    command = args[0] if args else ""

    if command == "create":
        create()
    elif command == "list":
        print(json.dumps(request("GET", URL_BASE), indent=2, ensure_ascii=False))
    elif command == "delete":
        if len(args) < 2:
            sys.exit("Usage: python scripts/webhook_endpoints.py delete <webhook_endpoint_id>")
        request("DELETE", f"{URL_BASE}/{args[1]}")
        print(f"Deleted webhook endpoint {args[1]}")
    else:
        sys.exit(
            "Usage:\n"
            "  python scripts/webhook_endpoints.py create        register WEBHOOK_PUBLIC_URL\n"
            "  python scripts/webhook_endpoints.py list          list registered endpoints\n"
            "  python scripts/webhook_endpoints.py delete <id>   delete one"
        )


if __name__ == "__main__":
    main()
