# Contextualised banking voice agent — Azure OpenAI GPT Realtime over SIP

A demo phone agent that **recognises the caller from their number before it answers**, greets them
by name in their preferred language, and can discuss their account. Ships with a web console for
managing the customer data and watching calls live.

Built on the
[OpenAI SIP guide](https://developers.openai.com/api/docs/guides/voice-sip?api=realtime),
adapted for [Azure OpenAI](https://learn.microsoft.com/azure/foundry/openai/how-to/realtime-audio-sip).

```
  ☎  Caller
     │
     ▼
  Vonage SIP trunk
     │  sip:proj_<internalId>@<region>.sip.ai.azure.com;transport=tls
     ▼
  Azure OpenAI Realtime (SIP)
     │  POST /webhook  { type: "realtime.call.incoming", data.call_id, data.sip_headers }
     ▼
  This app  ── 1. read the caller's number from the SIP "From" header
             ── 2. look the customer up in SQLite
             ── 3. POST .../accept with their profile in `instructions`
             ── 4. WSS .../realtime?call_id=... → response.create (greet by name)
```

The contextualisation happens **before** the call is accepted, because `sip_headers` arrives on the
webhook. The model therefore knows who it is talking to from its very first word, rather than being
told mid-conversation.

---

## What makes a call "contextualised"

When `+974 5551 2345` rings in, the agent is handed this at accept time:

```
You are the virtual assistant for a retail bank. You are speaking with a customer on the telephone.

CUSTOMER PROFILE
Name (English): Ahmed Al-Mansouri
Name (Arabic): أحمد المنصوري
Preferred language: English
Mobile: +97455512345
Segment: Premium
Home branch: West Bay
Account type: Current
Balance: QAR 48,320.55
Last transaction: QAR 1,250.00 at Lulu Hypermarket on 2026-09-14
Card status: Active
KYC: Valid (expires 2027-03-01)
Open case: Disputed charge QAR 899.00, ref CS-40281, status In review
Loan: Auto loan, QAR 62,000 outstanding, 18 instalments left
Relationship manager: Noura Al-Kuwari

You already know who is calling because the call came from their registered mobile number.
Greet Ahmed Al-Mansouri by name. Do not ask them to identify themselves again.

Hold the conversation in English. Switch language only if the caller asks or starts speaking
another language.

Rules you must follow:
- Use only the facts in the customer profile above. Never invent balances, transactions, dates,
  or case references.
- If you are asked something the profile does not cover, say you will check with a colleague and
  offer to follow up, rather than guessing.
- Never read out the full account balance until the caller has asked for it.
- Keep replies short and natural: this is a phone call, not a written chat.
```

An Arabic-preference customer gets the same profile with an Arabic language directive and an
Arabic greeting. An unrecognised number gets a neutral English greeting and is told plainly that
no account details are visible.

You can see exactly what any number would produce, without placing a call, from the
**Agent settings → Preview contextualisation** panel.

---

## Prerequisites

- An Azure subscription and a **Microsoft Foundry / Azure OpenAI resource** in **`swedencentral`**
  or **`eastus2`**.
  > ⚠️ SIP trunking works only in those two regions.
- A **realtime model deployment** (for example `gpt-realtime`).
  > ⚠️ The `model` field sent at accept time must be your **deployment name**, not the model name.
- Role assignment **Cognitive Services User** or **Cognitive Services Contributor**.
- A **Vonage** account with a voice-capable number.
- **Python 3.10 or later**.
- For local development, the [`devtunnel` CLI](https://learn.microsoft.com/azure/developer/dev-tunnels/get-started).

---

## Quick start

```powershell
git clone https://github.com/melrasheed/openai-sip-trunking
cd openai-sip-trunking
cp .env.example .env     # then fill in your Azure OpenAI values
.\run.ps1
```

```bash
./run.sh
```

`run.ps1` / `run.sh` create the virtual environment, install `requirements.txt`, create and seed
the database, and start the server. They are safe to re-run — the venv is built once, and
dependencies reinstall only when `requirements.txt` changes.

| | |
| --- | --- |
| `.\run.ps1` | Start on `PORT` from `.env` (default 8000) |
| `.\run.ps1 -Port 8100` | Override the port |
| `.\run.ps1 -Seed` | Replace the customers with fresh demo data first |
| `.\run.ps1 -Reinstall` | Recreate the virtual environment |

Bash equivalents: `./run.sh`, `PORT=8100 ./run.sh`, `./run.sh --seed`, `./run.sh --reinstall`.

Then open **<http://127.0.0.1:8000/>** for the console.

---

## Configuration

| Variable | Required | Description |
| --- | --- | --- |
| `AZURE_OPENAI_ENDPOINT` | ✅ | `https://<your-resource-name>.openai.azure.com` |
| `AZURE_OPENAI_API_KEY` | ✅ | Key 1 or Key 2 from **Keys and Endpoint** |
| `AZURE_OPENAI_DEPLOYMENT` | ✅ | Your realtime **deployment** name |
| `AZURE_OPENAI_WEBHOOK_SECRET` | ✅ | Signing secret from the webhook registration script |
| `AZURE_OPENAI_TRANSCRIBE_DEPLOYMENT` | – | Deployment name of a transcription model. Enables caller-side transcript |
| `WEBHOOK_PUBLIC_URL` | for registration | Public HTTPS URL of `/webhook` |
| `WEBHOOK_NAME` | – | Friendly name for the webhook endpoint |
| `PORT` | – | Defaults to `8000` |
| `DATABASE_PATH` | – | Defaults to `./data/bank.db` |
| `DEMO_CUSTOMER_MSISDN` | – | Your own number, written over the first seed record |
| `LOG_LEVEL` | – | `INFO` or `DEBUG` |
| `AGENT_INSTRUCTIONS` / `WELCOME_GREETING` / `AGENT_VOICE` | – | Defaults; the UI overrides these |

The server fails fast at startup and names any missing required variable.

---

## The web console

Served on the same port as the webhook, so one tunnel exposes both.

> ⚠️ **No authentication.** Anyone who can reach the URL — including through your dev tunnel — can
> read and edit the customer records. It is a demo console: keep the data fictional.

**Customers** — the records the agent matches against. Add, edit and delete, with Arabic names
rendered right-to-left. "Reseed demo data" restores the eight sample customers.

**Calls** — every call, with the number it came from, which customer it matched, the language
chosen, and the outcome. Select a call to read its transcript; a call still in progress updates
live (polled once a second).

**Agent settings** — base instructions, fallback greeting and voice. Saved to the database and read
at the start of every call, so edits apply to the **next** call without restarting. The preview
panel renders the full instructions for any number you type.

### Making your own phone recognised

The demo only lands if your own number is in the database. Either set `DEMO_CUSTOMER_MSISDN` in
`.env` before the first seed, or open **Customers**, click any record, and change the mobile number.

---

## Connecting a phone number with Vonage

### 1. Expose the webhook

```bash
devtunnel user login
devtunnel create sip-agent-yourname --allow-anonymous
devtunnel port create sip-agent-yourname -p 8000 --protocol http
devtunnel host sip-agent-yourname
```

`--allow-anonymous` is required: Azure's webhook sender cannot complete a dev tunnel login. Tunnel
IDs are public DNS names and therefore globally unique, so pick something distinctive.

### 2. Register the webhook with Azure OpenAI

Azure exposes webhook endpoints over REST only — there is no portal UI. Put the tunnel URL in
`.env` as `WEBHOOK_PUBLIC_URL` (including the `/webhook` path), then:

```bash
.venv/bin/python scripts/webhook_endpoints.py create     # .venv\Scripts\python on Windows
```

Copy the `signing_secret` it prints into `.env` as `AZURE_OPENAI_WEBHOOK_SECRET` — it is shown
once — and restart the server.

```bash
python scripts/webhook_endpoints.py list
python scripts/webhook_endpoints.py delete <id>
```

### 3. Find your project ID

In the Azure portal, open your Azure OpenAI resource, choose **JSON View**, and copy the internal
ID (32 hex characters). Your SIP destination is:

```
sip:proj_<internalId>@<region>.sip.ai.azure.com;transport=tls
```

where `<region>` is `swedencentral` or `eastus2`.

### 4. Point the Vonage trunk at it

In the [Vonage dashboard](https://dashboard.nexmo.com/sip), under **Voice → SIP**:

1. Create a SIP trunk. When asked what you are connecting to, choose **Something else** — the
   destination is Azure, not a supported PBX product.
2. Set the trunk's destination SIP URI to the `sip:proj_...@<region>.sip.ai.azure.com;transport=tls`
   address from step 3.
3. Assign your Vonage voice number to the trunk so inbound calls route to it.
4. Make sure the trunk uses **TLS** for signalling (port 5061, TLS 1.2 or later) and **SRTP** for
   media. Azure requires both; see the caveat below.

See Vonage's own [SIP trunking](https://developer.vonage.com/en/sip/sip-dashboard) and
[SIP technical details](https://developer.vonage.com/en/sip/technical-details) documentation for
the current dashboard flow.

> ⚠️ **TLS signalling requires SRTP media.** The `transport=tls` in the SIP URI encrypts
> *signalling* only. If the trunk then sends plain RTP, the call connects and signalling succeeds
> while audio fails — you get one-way or silent audio and a teardown after roughly ten seconds.
> Enable secure media on the Vonage trunk.

### 5. Call the number

Watch the log, or the **Calls** tab:

```
[rtc_...] incoming from +97455512345 -> Ahmed Al-Mansouri (language=en)
[rtc_...] accept -> 200
[rtc_...] websocket connected
```

---

## Testing without placing a call

```bash
python scripts/send_test_webhook.py --from +97455512345
```

This signs a `realtime.call.incoming` event exactly as Azure does and posts it at your local
server, so the signature check, the customer lookup and the accept request all run for real.

**Expect a `502` carrying `status: 404` — that is a pass**, and the script says so. The event
carries a synthetic `call_id` with no live SIP session behind it, so Azure declines to accept it.
Getting that far proves the webhook secret, endpoint and API key are all correct — a bad key would
return `401`, not `404`.

The server log then shows which customer the number matched. Try an unknown number to see the
fallback:

```bash
python scripts/send_test_webhook.py --from +14155550123
```

---

## Project layout

```
src/
  app.py                Flask: webhook, JSON API, UI route, realtime monitor
  db.py                 SQLite schema, seed data, queries
  context.py            SIP header parsing, number matching, profile builder
  templates/
    index.html          The console (inline CSS/JS, no build step)
scripts/
  webhook_endpoints.py  create | list | delete webhook endpoints
  send_test_webhook.py  Locally signed webhook for offline testing
infra/
  main.bicep            App Service plan + web app
data/
  bank.db               Created on first run (gitignored)
run.ps1 / run.sh        venv + dependencies + start
```

### How the caller is matched

Trunks differ in how much of the number they send, so matching is deliberately forgiving:

1. The user part is pulled from the SIP `From` URI — `sip:+97455512345@host` → `+97455512345`.
2. It is reduced to bare digits.
3. An exact match is tried first, then progressively shorter suffixes (11, 10, 9, 8, 7 digits).
   A suffix match is only accepted when exactly one customer matches, so an ambiguous match is
   treated as no match rather than a wrong one.

That means `+97455512345`, `97455512345`, `0097455512345` and `55512345` all find the same person,
while anonymous callers (`sip:anonymous@anonymous.invalid`) correctly find nobody.

---

## Caller-side transcript

The live transcript shows the assistant's side out of the box. To also capture what the **caller**
says, set `AZURE_OPENAI_TRANSCRIBE_DEPLOYMENT` to the name of a transcription model deployment
(for example a `gpt-4o-transcribe` deployment).

> Azure requires a **deployment name** in `input_audio_transcription`, unlike OpenAI which takes a
> model name. This is left opt-in so that a missing or wrong value cannot break an otherwise
> working accept request.

---

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| `400 Invalid signature` | `AZURE_OPENAI_WEBHOOK_SECRET` must match the secret from registration. It is shown only once — if lost, delete the endpoint and create it again. Restart after editing `.env`. |
| `accept -> 404 call_id_not_found` | Expected for `send_test_webhook.py`: a synthetic `call_id` has no live SIP session. On a **real** call it means the session ended before the accept arrived. |
| `accept -> 404` on a real call | The deployment in `AZURE_OPENAI_DEPLOYMENT` does not exist. Check the `code` in the response: `call_id_not_found` is about the call, not the deployment. |
| `accept -> 401` | Wrong `AZURE_OPENAI_API_KEY`, or the key belongs to a different resource than `AZURE_OPENAI_ENDPOINT`. |
| Calls never reach the webhook | Confirm the SIP URI host is `<region>.sip.ai.azure.com`, that `transport=tls` is present, and that the project ID is `proj_<32-hex>`. Check the registered URL with `webhook_endpoints.py list`. |
| Caller hears nothing, call drops after ~10 s | Media, not signalling. TLS signalling requires SRTP media — enable secure media on the Vonage trunk. |
| Caller is never recognised | Check the log line `incoming from <number>`. If the number differs from the stored one, edit the customer in the console — matching tolerates formatting but not a genuinely different number. |
| The agent invents account details | The guardrails tell it to use only the profile. Tighten the base instructions in **Agent settings**, which apply from the next call. |
| Tunnel worked yesterday, not today | Dev tunnels expire (30 days maximum). Recreate it and re-register the webhook URL. |

---

## Notes

- **All customer data is fictional.** Names, balances, cases and loans are invented for the demo.
- The `openai` package is used **only** to verify webhook signatures; no request is ever sent to
  `api.openai.com`.
- Only stdlib `sqlite3` is used for storage — there is no ORM and no database server to run.
- The Flask development server is fine for a demo. Put a real WSGI server in front of it before
  using this for anything else.

## License

ISC — see [LICENSE](LICENSE).
