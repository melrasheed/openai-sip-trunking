# Azure OpenAI SIP trunking

Answer inbound phone calls with the **Azure OpenAI (Microsoft Foundry) GPT Realtime API** over SIP.

A SIP trunking provider converts a PSTN call into IP traffic and routes it to your Foundry project.
Azure OpenAI then posts a `realtime.call.incoming` webhook to this server, which accepts the call,
attaches to the realtime session over WebSocket, and greets the caller.

```
  ☎  Caller
     │
     ▼
  SIP trunking provider (Twilio / Vonage / Bandwidth …)
     │  sip:proj_<internalId>@<region>.sip.ai.azure.com;transport=tls
     ▼
  Azure OpenAI Realtime (SIP)
     │  POST /webhook   { type: "realtime.call.incoming", data.call_id }
     ▼
  This server ──POST /openai/v1/realtime/calls/{call_id}/accept──▶ Azure OpenAI
     │
     └────────WSS /openai/v1/realtime?call_id={call_id}───────────▶ Azure OpenAI
                        (response.create → greeting, then server events)
```

Based on [Use the GPT Realtime API via SIP](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/realtime-audio-sip)
and [Azure OpenAI webhooks](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/webhooks).

---

## Prerequisites

- An **Azure subscription**.
- A **Microsoft Foundry / Azure OpenAI resource** in **`swedencentral`** or **`eastus2`**.
  > ⚠️ SIP trunking is only supported in these two regions. A resource anywhere else will never
  > receive SIP traffic.
- A **deployment of a realtime model** (for example `gpt-realtime`, `gpt-realtime-mini`,
  `gpt-4o-realtime-preview`). In the Foundry portal: **Build → Models → Deploy a base model**.
  > ⚠️ The `model` field sent when accepting a call must be your **deployment name**, not the
  > underlying model name. This is the most common migration mistake.
- Role assignment **Cognitive Services User** or **Cognitive Services Contributor** on the resource.
- An account with a **SIP trunking provider** and a **phone number** purchased from them.
- **Node.js 20 or later** for the TypeScript implementation, or **Python 3.10 or later** for the
  Python one.
- For local development: the [**`devtunnel` CLI**](https://learn.microsoft.com/azure/developer/dev-tunnels/get-started)
  (or any HTTPS tunnel), plus a Microsoft or GitHub account to sign in with.

---

## Configuration

```bash
npm install
cp .env.example .env
```

| Variable | Required | Description |
| --- | --- | --- |
| `AZURE_OPENAI_ENDPOINT` | ✅ | `https://<your-resource-name>.openai.azure.com` (no trailing path) |
| `AZURE_OPENAI_API_KEY` | ✅ | Key 1 or Key 2 from **Keys and Endpoint** |
| `AZURE_OPENAI_DEPLOYMENT` | ✅ | Your realtime **deployment** name |
| `AZURE_OPENAI_WEBHOOK_SECRET` | ✅ | Signing secret from `npm run webhook:create` |
| `WEBHOOK_PUBLIC_URL` | for registration | Public HTTPS URL of the `/webhook` route |
| `WEBHOOK_NAME` | – | Friendly name for the webhook endpoint |
| `ADMIN_API_TOKEN` | – | Enables the admin call-control routes. Empty ⇒ routes disabled |
| `PORT` | – | Defaults to `8000` |
| `WELCOME_GREETING` | – | Greeting spoken on answer |
| `AGENT_INSTRUCTIONS` | – | System instructions for the session |
| `AGENT_VOICE` | – | Output voice. Unset ⇒ omitted from the accept body, so Azure picks the default |
| `LOG_EVENT_PAYLOADS` | – | Log full JSON for every event and the accept request/response. Defaults to `true`; set `false` for concise logs |

The server fails fast at startup and names any missing required variable.

---

## Option A — Run locally with a Microsoft dev tunnel

Azure OpenAI must reach your webhook over public HTTPS, so a tunnel is required during development.
[Microsoft dev tunnels](https://learn.microsoft.com/azure/developer/dev-tunnels/overview) provide
one, with a stable URL and a built-in traffic inspector.

### 1. Install the `devtunnel` CLI

```powershell
winget install Microsoft.devtunnel
```

```bash
# macOS
brew install --cask devtunnel

# Linux
curl -sL https://aka.ms/DevTunnelCliInstall | bash
```

Dev tunnels cannot be hosted anonymously, so sign in once with a Microsoft or GitHub account:

```bash
devtunnel user login
```

### 2. Start the server

```bash
npm run dev
```

It listens on `http://0.0.0.0:8000`.

### 3. Create and host the tunnel

In a second terminal, create a **persistent** tunnel with a fixed ID so the public URL survives
restarts, add port 8000, and host it:

```bash
devtunnel create sip-realtime-yourname --allow-anonymous
devtunnel port create sip-realtime-yourname -p 8000 --protocol http
devtunnel host sip-realtime-yourname
```

```
Hosting port 8000 at https://sip-realtime-yourname-8000.uks1.devtunnels.ms/
   and inspect it at https://sip-realtime-yourname-8000-inspect.uks1.devtunnels.ms/
```

Copy the **hosting URL**. The `uks1` cluster segment depends on your region.

> ℹ️ Tunnel IDs become public DNS names, so they are **globally unique** — `devtunnel create` fails
> if the ID is already taken. Add something distinctive (your name, a random suffix), or omit the ID
> entirely to get a generated one. Use lowercase letters, digits, and hyphens.

> ⚠️ `--allow-anonymous` is **required**. Azure OpenAI's webhook sender cannot complete a dev
> tunnel login, so without it every delivery is bounced with a sign-in redirect. Anyone who guesses
> the tunnel ID can reach your local server, so pick a non-obvious ID and stop the tunnel when
> you're done.

For a throwaway tunnel instead, a single command does everything — but it is deleted on exit and
you get a **new URL every time**, which means re-registering the webhook endpoint on each run:

```bash
devtunnel host -p 8000 --allow-anonymous
```

### 4. Register the webhook endpoint

Azure OpenAI webhook endpoints can only be created through the REST API — there is no portal UI.
Set the tunnel URL in `.env` (note the `/webhook` path):

```bash
WEBHOOK_PUBLIC_URL=https://sip-realtime-yourname-8000.uks1.devtunnels.ms/webhook
```

Then register it:

```bash
npm run webhook:create
```

```jsonc
{
  "id": "we_...",
  "object": "webhook.endpoint",
  "event_types": ["realtime.call.incoming"],
  "name": "sip-realtime-listener",
  "signing_secret": "whsec_...",   // ← shown ONCE, never retrievable again
  "url": "https://sip-realtime-yourname-8000.uks1.devtunnels.ms/webhook"
}
```

Copy `signing_secret` into `.env` as `AZURE_OPENAI_WEBHOOK_SECRET`, then restart the server so it
picks up the new value.

### 5. Verify the webhook plumbing — without placing a call

```bash
npm run webhook:test
```

This posts a correctly signed `realtime.call.incoming` event at your local server, exercising
signature verification and the accept call.

**Expect a `502` carrying `call_id_not_found` — that is a pass**, and the script says so:

```
  -> 502 Bad Gateway {"error":"Accept failed","status":404,"code":"call_id_not_found"}

PASS: signature verified, and the accept request reached Azure OpenAI.
```

The event carries a synthetic `call_id` with no live SIP session behind it, so Azure declines to
accept it. Reaching that error is exactly what the test is for — it proves the signature was
accepted, the accept URL is correct, and your API key authenticated (a bad key returns `401`, not
`404`). The script exits `0`.

> ℹ️ `AZURE_OPENAI_DEPLOYMENT` is **not** validated by this test, because Azure checks `call_id`
> before it looks at the model. A wrong deployment name only surfaces on a real call.

To prove the tunnel itself forwards traffic, aim the same script at the public URL:

```bash
npx tsx scripts/send-test-webhook.ts https://sip-realtime-yourname-8000.uks1.devtunnels.ms/webhook
```

You can also confirm rejection of forged requests:

```bash
curl -i -X POST http://127.0.0.1:8000/webhook \
  -H "Content-Type: application/json" \
  -H "Webhook-ID: test-id" \
  -H "Webhook-Timestamp: $(date +%s)" \
  -H "Webhook-Signature: v1,not-a-real-signature" \
  -d '{"type":"realtime.call.incoming","data":{"call_id":"test"}}'
# HTTP/1.1 400 Bad Request → Invalid signature
```

### 6. Point your SIP trunk at the project

1. In the **Azure portal**, open your Azure OpenAI resource and select **JSON View** to find the
   resource **internal ID** (a 32-character hex string).
2. Build the project ID: `proj_<internalId>` — for example
   `proj_88c4a88817034471a0ba0fcae24ceb1b`.
3. Configure your SIP trunking provider to route the phone number to:

   ```
   sip:proj_<internalId>@<region>.sip.ai.azure.com;transport=tls
   ```

   where `<region>` is `swedencentral` or `eastus2`.

### 7. Call the number

Watch the server logs:

```
[rtc_…] incoming call
[rtc_…] websocket open
[rtc_…] <- session.created
[rtc_…] <- response.created
```

Inspect the webhook deliveries that crossed the tunnel — headers, body, status — at the
**inspect URL** printed by `devtunnel host`:

```
https://sip-realtime-yourname-8000-inspect.uks1.devtunnels.ms/
```

### Managing the tunnel

```bash
devtunnel list                            # show your tunnels and their expiry
devtunnel show sip-realtime-yourname      # details, including the public URL
devtunnel host sip-realtime-yourname      # resume hosting — same URL as before
devtunnel delete sip-realtime-yourname    # tear it down when finished
```

> ⚠️ **Dev tunnels expire.** The maximum lifetime is 30 days; set it explicitly with
> `devtunnel create <id> -a --expiration 30d`. When a tunnel expires or is deleted, the registered
> webhook endpoint points at a dead URL and deliveries silently stop. Re-register it:
>
> ```bash
> npm run webhook:list                 # find the stale endpoint id
> npm run webhook:delete -- we_xxx     # remove it
> # update WEBHOOK_PUBLIC_URL in .env, then
> npm run webhook:create               # yields a NEW signing secret
> ```

> ℹ️ Dev tunnels show an anti-phishing interstitial for interactive browser navigation, but it does
> not apply to webhook traffic: the page is skipped for any non-`GET` request and for requests whose
> `Accept` header is not `text/html`.

---

## Option B — Deploy to Azure App Service

### With the provided Bicep template

```bash
az group create --name rg-sip-realtime --location swedencentral

az deployment group create \
  --resource-group rg-sip-realtime \
  --template-file infra/main.bicep \
  --parameters infra/main.parameters.json \
  --parameters appName=<globally-unique-app-name> \
               azureOpenAiEndpoint=https://<your-resource-name>.openai.azure.com \
               azureOpenAiApiKey=<your-api-key> \
               azureOpenAiWebhookSecret=<placeholder-for-now>
```

Secrets are declared `@secure()` and are deliberately **not** stored in
`infra/main.parameters.json`. Pass them on the command line, or harden further with
[Key Vault references](https://learn.microsoft.com/azure/app-service/app-service-key-vault-references).

The deployment outputs `webhookUrl`. Deploy the code, then register that URL:

```bash
az webapp up --name <globally-unique-app-name> --resource-group rg-sip-realtime --runtime "NODE:20-lts"

# Register the production webhook URL and capture the signing secret
WEBHOOK_PUBLIC_URL=https://<app-name>.azurewebsites.net/webhook npm run webhook:create

az webapp config appsettings set \
  --name <globally-unique-app-name> --resource-group rg-sip-realtime \
  --settings AZURE_OPENAI_WEBHOOK_SECRET="whsec_..."

az webapp restart --name <globally-unique-app-name> --resource-group rg-sip-realtime
az webapp log tail --name <globally-unique-app-name> --resource-group rg-sip-realtime
```

Finally, repoint your SIP trunk as in **Option A step 5** (the SIP URI is unchanged — it targets the
Foundry project, not this server).

Notes:

- The App Service **WebSockets** setting does **not** need enabling. It governs *inbound*
  WebSockets; this app only dials *outbound* to Azure OpenAI.
- `SCM_DO_BUILD_DURING_DEPLOYMENT=true` makes Oryx run `npm install` and `npm run build`, producing
  `dist/` for `npm start`.
- `healthCheckPath` is wired to `/health`.

---

## HTTP surface

| Method | Route | Auth | Purpose |
| --- | --- | --- | --- |
| `GET` | `/health` | none | Liveness probe |
| `POST` | `/webhook` | webhook signature | Receives `realtime.call.incoming` |
| `POST` | `/` | webhook signature | Alias of `/webhook` |
| `POST` | `/calls/:callId/reject` | `x-admin-token` | Decline a call |
| `POST` | `/calls/:callId/refer` | `x-admin-token` | Transfer a call |
| `POST` | `/calls/:callId/hangup` | `x-admin-token` | Disconnect a call |

The call-control routes can drop or redirect a **live** call, so they return `404` unless
`ADMIN_API_TOKEN` is set, and `401` unless the `x-admin-token` header matches.

```bash
# Decline with SIP 486 Busy Here (default without status_code is 603 Decline)
curl -X POST http://127.0.0.1:8000/calls/$CALL_ID/reject \
  -H "x-admin-token: $ADMIN_API_TOKEN" -H "Content-Type: application/json" \
  -d '{"status_code": 486}'

# Transfer to a human
curl -X POST http://127.0.0.1:8000/calls/$CALL_ID/refer \
  -H "x-admin-token: $ADMIN_API_TOKEN" -H "Content-Type: application/json" \
  -d '{"target_uri": "tel:+14155550123"}'

# Hang up
curl -X POST http://127.0.0.1:8000/calls/$CALL_ID/hangup \
  -H "x-admin-token: $ADMIN_API_TOKEN"
```

To turn calls away *before* answering, add the decision inside `handleWebhook` in
`src/index.ts` — `event.data.sip_headers` carries `From`, `To`, and `Call-ID`.

---

## Project layout

Two interchangeable implementations of the same server. Both read the same `.env`, expose the
same `/webhook` route, and follow the
[OpenAI SIP guide](https://developers.openai.com/api/docs/guides/voice-sip?api=realtime):
verify the webhook, `POST .../accept`, open the monitoring WebSocket, send `response.create`.
The only deviations are the three Azure requires — the `/openai/v1` base URL, the `api-key`
header, and `model` being a deployment name.

```
src/
  index.ts              TypeScript implementation (Express + ws)
  app.py                Python implementation (Flask + websockets)
scripts/
  webhook-endpoints.ts  create | list | delete webhook endpoints
  send-test-webhook.ts  Locally signed webhook for offline verification
infra/
  main.bicep            App Service plan + web app
  main.parameters.json  Non-secret parameters
run.ps1 / run.sh        Create the venv, install requirements, start the Python server
requirements.txt        Python dependencies
```

Run **one** of them at a time — they both bind the same port.

### Running the Python implementation

`run.ps1` (Windows) and `run.sh` (macOS/Linux) create `.venv`, install `requirements.txt`, and
start the server. They are safe to re-run: the virtual environment is created once, and
dependencies are reinstalled only when `requirements.txt` changes.

```powershell
.\run.ps1                # start on PORT from .env (default 8000)
.\run.ps1 -Port 8100     # override the port
.\run.ps1 -Reinstall     # recreate the virtual environment
```

```bash
./run.sh
PORT=8100 ./run.sh
./run.sh --reinstall
```

To run it by hand instead:

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt   # .venv\Scripts\pip on Windows
.venv/bin/python -u src/app.py
```

`npm run webhook:test` and the webhook management scripts work against either implementation.

### npm scripts

| Script | Purpose |
| --- | --- |
| `npm run dev` | Watch mode via `tsx` |
| `npm run build` | Compile to `dist/` |
| `npm start` | Run the compiled server |
| `npm run typecheck` | Type-check `src/` **and** `scripts/` |
| `npm run webhook:create` | Register `WEBHOOK_PUBLIC_URL` |
| `npm run webhook:list` | List webhook endpoints |
| `npm run webhook:delete -- <id>` | Delete a webhook endpoint |
| `npm run webhook:test` | Send a signed test webhook locally |

The `openai` package — in both Node and Python — is used solely for Standard Webhooks signature
verification; no request is ever sent to `api.openai.com`.

---

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| `400 Invalid signature` | `AZURE_OPENAI_WEBHOOK_SECRET` must match the secret from webhook creation. It is shown only once — if lost, delete the endpoint and re-create it. Restart the server after changing `.env`: dotenv reads it only at startup, and `tsx watch` does **not** reload on `.env` edits. |
| `accept failed: 404 call_id_not_found` | Expected when replaying a synthetic event (`npm run webhook:test`) — there is no live SIP session for a made-up `call_id`. On a **real** call it means the session ended before the accept arrived, usually because the handler took too long. |
| Calls never reach the webhook | Confirm the SIP URI host is `<region>.sip.ai.azure.com`, that `transport=tls` is present, and that the project ID is `proj_<32-char-hex>`. Check the registered URL with `npm run webhook:list`. |
| `Accept failed: 404` | The deployment name in `AZURE_OPENAI_DEPLOYMENT` does not exist on the resource. Check the `code` in the response body: `call_id_not_found` is about the call, not the deployment. |
| `Accept failed: 401` | Bad `AZURE_OPENAI_API_KEY`, or the key belongs to a different resource than `AZURE_OPENAI_ENDPOINT`. |
| Call connects but the caller hears nothing | Audio travels on the SIP media path, not the WebSocket, so check media first. The SIP URI uses `transport=tls`, which requires **encrypted media (SRTP)**; a trunk that signals over TLS but sends plain RTP connects the call while media fails. In the Twilio console enable **Elastic SIP Trunking → your trunk → General → Secure Trunking**, and allow bidirectional UDP to Azure's SRTP media range. |
| WebSocket closes with `1000 observer_writer_exit` | A symptom, not a cause: the call ended, so the monitoring session was torn down. Look **above** it in the log for a `response.done` whose `status` is not `completed`, or an `error` event. |
| The AI greets, the caller replies, but no answer ever comes | Look for `input_audio_buffer.speech_started` **without** a matching `speech_stopped`. VAD never saw the turn end, so nothing was committed and no response was generated — usually continuous line noise holding VAD open. |
| Call drops as soon as the caller speaks | The model's reply failed. Check the `response.done` payload: `status_details` carries the reason. Deployment quota and Azure content filtering are the usual causes. |
| WebSocket never opens | Ensure `wss://` (derived automatically from an `https://` endpoint) and that the `call_id` belongs to an accepted call. |
| Webhook deliveries time out | Keep handler work under ~10 seconds; the monitoring socket is opened after the accept returns and does not block the acknowledgement. |
| Tunnel reachable in a browser, but Azure never calls it | The tunnel is not anonymous, so deliveries hit a sign-in redirect. Re-host with `--allow-anonymous`, or run `devtunnel access create <tunnel-id> --anonymous`. |
| Tunnel deliveries stopped working after a break | The dev tunnel expired (30 day maximum) or was deleted, so the registered webhook URL is dead. Recreate the tunnel and re-register the endpoint. |
| `devtunnel host` fails to start | Run `devtunnel user login`; dev tunnels cannot be hosted anonymously even when client access is anonymous. |

### Common SIP status codes

| Code | Meaning | Resolution |
| --- | --- | --- |
| `486` | Busy Here | System overloaded; implement retry logic |
| `603` | Decline | Call explicitly rejected; check your accept/reject logic |
| `408` | Request Timeout | Network issue; verify connectivity to Azure |

---

## License

ISC — see [LICENSE](LICENSE).
