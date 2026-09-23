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
| `TRANSCRIPTION_MODEL` | – | Seeds the caller-transcription model on a fresh database. Defaults to `whisper`; the console owns it afterwards |
| `WEBHOOK_PUBLIC_URL` | for registration | Public HTTPS URL of `/webhook` |
| `WEBHOOK_NAME` | – | Friendly name for the webhook endpoint |
| `PORT` | – | Defaults to `8000` |
| `DATABASE_PATH` | – | Defaults to `./data/bank.db` |
| `DEMO_CUSTOMER_MSISDN` | – | Your own number, written over the first seed record |
| `LOG_LEVEL` | – | `INFO` or `DEBUG` |
| `AGENT_INSTRUCTIONS` | – | Seeds the role sentence of the prompt template on a fresh database; the console owns it afterwards |
| `AZURE_SEARCH_ENDPOINT` | for the knowledge base | `https://<search-service>.search.windows.net` |
| `AZURE_SEARCH_KNOWLEDGE_BASE` | for the knowledge base | Name of the knowledge base — not the index, and not a knowledge source |
| `AZURE_SEARCH_API_KEY` | for the knowledge base | Admin or query key for the search service |
| `AZURE_SEARCH_API_VERSION` | – | Defaults to `2026-08-01-preview` |
| `AZURE_SEARCH_MCP_URL` | – | Overrides the derived MCP URL outright |
| `AZURE_SEARCH_MCP_SERVER_LABEL` | – | Overrides the label the agent sees for the server |

The server fails fast at startup and names any missing required variable.

---

## The web console

Styled after [Commercial Bank](https://www.cbq.com.qa/en) and served on the same port as the
webhook, so one tunnel exposes both. Light and dark themes, and an Arabic toggle that flips the
whole console to right-to-left.

> ⚠️ **No authentication.** Anyone who can reach the URL — including through your dev tunnel — can
> read and edit the customer records. It is a demo console: keep the data fictional.

**Overview** — headline numbers (customers, calls today, recognition rate, average duration) and a
live indicator while a call is in progress.

**Customers** — the records the agent matches against, searchable by name or number. Selecting a
row opens a slide-over editor grouped into Identity, Contact and language, Account, and Activity
and service. Arabic names render right-to-left, and an **Arabic style** selector appears when the
preferred language is Arabic. "Reseed" restores the eight sample customers.

**Calls** — every call, with the number it came from, which customer it matched, and the outcome.
The table refreshes itself every few seconds (and pauses while the tab is hidden), so there is
nothing to press. Selecting a call shows the transcript as a conversation; a call in progress
updates live, and an indicator shows whether caller transcription is arriving.

**Agent settings** — the basic and advanced realtime settings, the delivery style, the full
editable system prompt, and the Arabic style prompts. The preview panel renders the exact
instructions and audio payload that any number would produce, without placing a call.

Each view is deep-linkable: `?view=customers`, `?view=calls`, `?view=settings`.

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

The knowledge base follow-up and the currency wording are pure logic, so they can be checked
without a call at all:

```bash
python scripts/check_knowledge_flow.py
```

This drives the follow-up state machine through every event order that matters — the search
finishing before the response and after it, the caller interrupting, a search that never comes
back — and confirms no bare currency code reaches the model for any seeded customer.

---

## Project layout

```
src/
  app.py                Flask: webhook, JSON API, console route, realtime monitor
  db.py                 SQLite schema, migrations, seed data, queries
  context.py            SIP header parsing, number matching, profile and dialect assembly
  knowledge.py          Azure AI Search knowledge base as a remote MCP tool
  settings_spec.py      Realtime settings: defaults, ranges, accept-payload construction
  prompts/
    system_template.txt The whole system prompt, with {placeholders} for the per-caller sections
    faseeh_arabic.txt   Modern Standard Arabic style prompt
    qatari_dialect.txt  Doha dialect style prompt
  templates/
    index.html          The console (inline CSS/JS, no build step)
scripts/
  webhook_endpoints.py  create | list | delete webhook endpoints
  send_test_webhook.py  Locally signed webhook for offline testing
  check_knowledge_flow.py  Knowledge base follow-up and currency wording, checked offline
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

## Agent settings

Everything below lives in **Agent settings** in the console. Values are stored in the database and
read at the start of every call, so a change applies to the **next** call without a restart. Only
values that differ from the service default are sent, keeping the accept payload minimal.

### Basic

| Setting | Default | What it does |
| --- | --- | --- |
| Bank name · English | `Commercial Bank of Qatar` | How the agent refers to the bank when speaking English |
| Bank name · Arabic | `البنك التجاري` | How the agent refers to the bank when speaking Arabic |
| Voice · English caller | `marin` | Voice used when the caller's preferred language is English |
| Voice · Arabic caller | `cedar` | Voice used when the caller's preferred language is Arabic |
| Delivery style | `Professional` | How the agent should sound. Folded into the system prompt as `{style}` |
| Playback speed | `1.0` | Speed of the agent's speech. Range `0.25`–`1.5` |
| Transcription model | `whisper` | Transcribes the caller. Empty disables caller transcription |

There is no transcription language setting: the hint follows the caller. A matched customer's
preferred language is sent as `audio.input.transcription.language` (`ar` or `en`), and an
unrecognised caller is left to the service's own detection rather than being guessed at.

#### Delivery styles

| Style | Effect |
| --- | --- |
| **Professional** | Polished, composed and efficient; courteous and businesslike |
| **Friendly** | Warm and conversational, like a helpful colleague |
| **Empathetic** | Patient and reassuring; acknowledges feelings first. Suits complaints |
| **Energetic** | Upbeat and enthusiastic, with lively pacing |
| **Concise** | The fewest words that answer the question |
| **Formal** | Reserved and highly deferential; no colloquialisms or contractions |

The style applies to both languages. An Arabic style set on a customer's profile still has the
final word on register, because the template places `{dialect}` after `{style}`.

### Advanced

Collapsed by default, since the demo path rarely needs it.

| Setting | Default | What it does |
| --- | --- | --- |
| Turn detection | `server_vad` | `server_vad` splits on silence, `semantic_vad` waits until the caller sounds finished, `none` never cuts in automatically |
| Speech threshold | `0.5` | How loud speech must be to register. Range `0`–`1`. Raise on a noisy line |
| Prefix padding | `300` ms | Audio kept from before speech starts, so the first syllable is not clipped |
| End-of-turn silence | `200` ms | Silence before the caller's turn is considered finished |
| Semantic eagerness | `auto` | Semantic VAD only — how readily the model decides the caller has finished |
| Reply automatically | on | Off makes the agent wait rather than answering on end of speech |
| Allow caller to interrupt | on | Lets the caller talk over the agent |
| Max reply tokens | `0` (no limit) | Caps the length of a single reply |

Shapes and ranges above were verified against the live service. Worth knowing if you extend this:
`temperature` and `max_response_output_tokens` are **rejected** by the GA realtime session —
`max_output_tokens` is the supported spelling — and turn detection is switched off by sending
`null`, not `{"type": "none"}`.

---

## The final system prompt

There is no hidden preamble. **Agent settings → Final system prompt** (collapsed, because it is
advanced) holds the *entire* string sent as `instructions` when a call is accepted, and it is
editable. The per-caller parts are placeholders, filled in at accept time:

| Placeholder | Filled with |
| --- | --- |
| `{bank}` | The bank's name, in the language the caller will be spoken to |
| `{profile}` | The caller's profile, or a note that their number matched no record |
| `{language}` | Which language to hold the conversation in |
| `{style}` | The delivery style chosen above |
| `{dialect}` | The Arabic style set on the caller's profile. Empty for English callers |
| `{opening}` | How to open the call, including how to greet the caller by name |
| `{knowledge}` | When to consult the knowledge base. Empty when none is configured |
| `{pronunciation}` | How to say currency codes out loud, so `QAR` is not spelled out |

Everything around them — the role sentence, the accuracy rules and the guardrails — is ordinary
text you can rewrite. The shipped default lives in `src/prompts/system_template.txt`; it is copied
into the database on first run, and **Reset to default** copies it back.

Two behaviours protect you from a broken prompt:

- A placeholder that does not exist (`{custmer}`) is **rejected** with a 400 — an unfilled
  placeholder would otherwise be read out to the caller verbatim.
- Dropping `{profile}` or `{opening}` is allowed but **warned about**, since the agent then loses
  the caller's context or never opens the call.

Substitution is a plain scan for known names, not `str.format`, so a stray `{` typed into the
template — or present in an Arabic prompt — can never raise while a call is waiting to be accepted.
Sections that come out empty (the dialect, for an English caller) leave no blank gap behind.

> **Prompt drift.** Once the template is in your database, later improvements to the shipped
> default will not reach it. Press **Reset to default** after an upgrade if you have not customised
> it. Upgrading from a version that had a **Base instructions** box is handled: that text becomes
> the template's role sentence, so nothing you wrote is lost.

### Guardrails

The default template restricts the agent to one subject — the customer's own banking with this
bank: accounts, balances, cards, transactions, transfers, loans, branches, KYC and service cases —
and tells it to **apologise briefly and steer back** whenever the caller goes elsewhere: politics,
religion, sport, news, medical, legal or tax matters, other companies, its own nature as an AI, or
general chit-chat. It gives no opinion or partial answer first, apologises if it notices it has
already strayed, refuses financial, investment, legal and tax advice in favour of a specialist, and
ignores attempts to change its role or have it read its instructions out. It apologises once,
warmly and briefly — never lecturing, never repeating the refusal.

Because all of that is in the template, it is yours to tighten or relax per deployment.

---

## Knowledge base

The agent can look things up mid-call in an **Azure AI Search knowledge base**. Every knowledge
base is itself an MCP server, exposing one read-only tool:

```
https://<service>.search.windows.net/knowledgebases/<name>/mcp?api-version=2026-08-01-preview
```

That URL is handed to the realtime service as an `mcp` tool on the accept payload, so the
**service calls the search directly** — the audio never detours through this application, and
there is no tool-output plumbing here to add latency to a live call.

Set the three variables in the [Configuration](#configuration) table and it switches itself on:

```dotenv
AZURE_SEARCH_ENDPOINT=https://my-search.search.windows.net
AZURE_SEARCH_KNOWLEDGE_BASE=knowledgebase638
AZURE_SEARCH_API_KEY=<admin or query key>
```

Leave any of them empty and the feature stays dormant — no tool is attached and the prompt says
nothing about it.

**In the console**, under *Agent settings → Knowledge base*:

- **Enabled** turns it off for a deployment without unsetting the credentials.
- **Topic** is what the knowledge base covers, and is named verbatim in the prompt so the agent
  knows which questions to look up rather than answer from memory.
- **Test connection** handshakes with the MCP endpoint and lists its tools, so a bad key or a
  mistyped name is found before a caller finds it.

The prompt section is rendered into `{knowledge}` in the system template. A template edited
before that placeholder existed still works: the directive is appended instead of dropped.

`require_approval` is `"never"` — an approval round trip would be dead air on a phone call — and
`allowed_tools` narrows the agent to `knowledge_base_retrieve` alone.

### If the search feels slow

The default `2026-08-01-preview` API version **synthesizes** an answer with a language model
before returning it, which is why a lookup takes ten to fifteen seconds. Setting
`AZURE_SEARCH_API_VERSION=2026-04-01` switches to plain extractive retrieval: materially faster,
but the agent gets passages rather than a composed answer and has to do the composing itself.

Worth trying if the holding phrases feel like too much of a wait. Leave it alone if you prefer
the answer quality — the wait is already covered.

### A note on the key

The search key travels in an `api-key` header inside the accept payload. `/api/preview` redacts
it, and it is never written to the transcript or the log. For production prefer a bearer token
from managed identity with the **Search Index Data Reader** role over an admin key.

### Watching it work

Knowledge base activity shows up in the call transcript as `system` lines, and in the log:

```
[call_…] knowledge base connected
[call_…] knowledge base query: {"query":"credit card annual fee"}
[call_…] search still running, holding phrase 1
[call_…] knowledge base search completed
[call_…] knowledge base ready, asking for the answer
```

If the realtime deployment rejects the MCP tool outright, the call is **not** dropped: the accept
is retried without it and the transcript records that the call is continuing without the
knowledge base.

### Why the answer needs a second nudge

A remote MCP tool is run by the Realtime **service**, not by this app, so there is no result for
us to hand back the way a local function tool would. The realtime guide is blunt about the
consequence:

> After the response is done and all of its MCP calls have finished, send another `response.create`
> event to let the model use the results and proceed with the conversation. **The Realtime API
> doesn't create these follow-up responses automatically.**

Left alone, that is a bug you can hear: the agent says it will check, the search runs, and the
line goes quiet until the caller asks again. `_mcp_tick()` in `src/app.py` sends that follow-up.

Two details shape it. `response.done` can arrive *before* the search finishes, so this is not a
sequence but a small state machine that fires when both halves are true, in whichever order they
land. And the follow-up is deliberately sent **bare**, with no `instructions`: per-response
instructions replace the session instructions wholesale, which would strip the caller's language,
dialect and profile out of the one reply that most needs them.

The follow-up also waits for the floor. If the caller is mid-sentence when the results land, the
answer is held until they finish rather than spoken over them.

### Keeping the caller company

A knowledge base search can take ten to fifteen seconds, which is a long time to hold a phone to
your ear in silence. While one is running the agent speaks short holding phrases — "still checking
that for you" — scheduled off the same state machine and worded in the caller's own language and
dialect. These are capped, so a slow lookup does not turn into chatter.

| Setting | Default | What it does |
| --- | --- | --- |
| **Speak holding phrases** | on | Whether to fill the silence at all |
| **First holding phrase after** | 5000 ms | Delay before the first one; the agent's own reply usually covers the first few seconds |
| **Holding phrase interval** | 5000 ms | Gap between them |
| **Maximum holding phrases** | 2 | Ceiling per search |
| **Search timeout** | 30000 ms | When to give up and answer without the search |

Keep the timeout comfortably above the real search time. Set it near ten seconds and a healthy
lookup gets abandoned mid-flight.

---

## Saying money out loud

Left to itself the model reads `QAR 4,820.00` as "Q-A-R", spelling the code out letter by letter.
On a phone call that is simply wrong. Two layers fix it, because one alone would not:

- **Structured fields** — `_money()` in `src/context.py` renders amounts as words from the start,
  so a balance reaches the model as `48,320.55 Qatari riyals`, never as a code.
- **Free text** — seeded fields like `Auto loan, QAR 62,000 outstanding` carry the code inside a
  sentence, well out of `_money()`'s reach. `expand_currency_codes()` rewrites those as the profile
  is built.

A `{pronunciation}` rule is added to the prompt as a backstop for anything the agent says from its
own knowledge. Like `{knowledge}`, it is appended automatically when a stored template predates the
placeholder, so an edited prompt keeps working without being overwritten.

QAR, USD, EUR, GBP, AED and SAR are known by name; any other code falls back to the written form.

---

## Arabic styles

A customer whose preferred language is Arabic can be given a delivery style, chosen on their
profile:

| Style | Effect |
| --- | --- |
| **Model default** | Leaves the model's own Arabic untouched |
| **Faseeh** | Enforces Modern Standard Arabic — full case endings, no dialectal vocabulary, formal register |
| **Qatari** | Enforces the Doha dialect — qaf rules, gender-aware address forms (چ / ك), mandatory substitutions |

The prompt behind each style is seeded into the database from `src/prompts/` and is editable in
**Agent settings → Arabic styles** (collapsed by default — it is advanced configuration), so the
wording can be tuned without touching code. The selected style is substituted into the template
after the customer profile and after the delivery style, so the agent knows who it is speaking to,
*and* the dialect's register rules are the last word on how it sounds.

> **Upgrading an existing database:** the `arabic_variant` column is added in place, and every
> existing customer keeps the **Model default** style — an upgrade will not silently rewrite records
> you have edited. To see the dialects, either press **Reseed** or set the style on a customer.
> The server log makes the choice visible on each call:
> `incoming from +974… -> Mohammed Al-Thani (language=ar style=qatari)`.

### How the agent opens the call

There is no separate greeting string. The instruction to open the call lives inside the same
accept-time `instructions` as the profile, language directive and Arabic style, and the WebSocket
sends a bare `response.create`:

```javascript
ws.send(JSON.stringify({ type: "response.create" }));
```

This matters. `response.create` carrying its own `instructions` **replaces** the session
instructions for that one response — so an opening line configured that way is generated *without*
the customer profile, the guardrails or the Arabic style. The symptom is subtle and easy to miss:
the first sentence is in plain Arabic and every sentence afterwards is in the correct dialect.

The opening directive also describes intent rather than supplying a sentence to copy, so the active
style owns the wording instead of competing with a hardcoded example.

---

## Caller-side transcript

On by default. `TRANSCRIPTION_MODEL` is `whisper`, and the transcript records both sides of the
conversation.

No separate transcription deployment is required. The Azure reference documentation notes that
`input_audio_transcription.model` takes a deployment name, which suggests one must be created
first — testing against a live resource shows otherwise:

- `whisper` resolves in the same `/openai/v1/models` namespace as the realtime deployment that is
  already working.
- Session configuration is **not** validated when the call is accepted, so this block cannot cause
  an accept to fail.

The failure mode is therefore silent rather than fatal: a value the service cannot resolve simply
produces no caller transcript. The Calls view shows **caller transcription active / not received**
for each call so a misconfiguration is visible rather than mysterious.

Azure documents **two spellings** for the transcription events, and a deployment may emit either,
or stream deltas instead of one completed event. All of these are handled:

| Event | Handling |
| --- | --- |
| `conversation.item.input_audio_transcription.completed` | Recorded as a caller line |
| `conversation.item.audio_transcription.completed` | Recorded as a caller line |
| `…transcription.delta` | Accumulated per item and flushed when the item completes |
| `…transcription.failed` | Error written into the transcript and the log |
| Anything else containing `transcription` with a `transcript` | Recorded anyway |

Transcription events are logged in full, so if the caller transcript is still missing the log says
which event arrived and what the service reported.

If `.failed` says the model cannot be resolved, change **Transcription model** in the console —
`gpt-4o-transcribe` is also available on this resource.

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
| The agent invents account details | The guardrails tell it to use only the profile. Tighten them in **Agent settings → Final system prompt**, which applies from the next call. |
| Knowledge base shows **not configured** | One of `AZURE_SEARCH_ENDPOINT`, `AZURE_SEARCH_KNOWLEDGE_BASE` or `AZURE_SEARCH_API_KEY` is empty. All three are needed, and `.env` is read at startup — restart after editing it. |
| **Test connection** returns `HTTP 401` or `403` | Wrong `AZURE_SEARCH_API_KEY`, or the key belongs to a different search service than `AZURE_SEARCH_ENDPOINT`. |
| **Test connection** says the server does not expose `knowledge_base_retrieve` | `AZURE_SEARCH_KNOWLEDGE_BASE` is pointing at an index or a knowledge *source*. Only a knowledge base has an MCP endpoint. |
| Transcript says the call continued without the knowledge base | The realtime deployment rejected the `mcp` tool; the accept was retried without it. Check the preceding `accept rejected with knowledge base` log line for the reason. |
| The agent never searches the knowledge base | Check `{knowledge}` survives in **Final system prompt**, that **Enabled** is on, and that **Topic** actually describes what the caller is asking about. |
| The agent says it will check, then goes silent until nudged | The follow-up `response.create` is not reaching the service. Look for `knowledge base ready, asking for the answer` in the log; if the search never finished you will see the timeout line instead. |
| The agent talks over the caller with the answer | It should not — the follow-up waits for the floor. If it happens, the service is not sending `input_audio_buffer.speech_started`, so the app cannot tell the caller has started. |
| Too much chatter while waiting | Lower **Maximum holding phrases**, raise **Holding phrase interval**, or turn **Speak holding phrases** off in **Agent settings → Knowledge base**. |
| Searches are abandoned before they finish | **Search timeout** is too close to the real search time. Raise it; the default of 30 s already allows for a slow lookup. |
| The agent spells out "Q-A-R" | The `{pronunciation}` rule is missing from the stored prompt. It is appended automatically, so check **Final system prompt** for a line starting `Pronunciation:`. |
| The agent chats about anything asked | The scope rules live in the template. Check they are still there — **Reset to default** restores them — and remember an edit applies from the next call, not the one in progress. |
| The first sentence ignores the Arabic style | Something is sending `instructions` on `response.create`; that replaces the session instructions for that one response. The opening belongs in the template's `{opening}`. |
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
