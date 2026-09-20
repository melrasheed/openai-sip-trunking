/**
 * Answers inbound SIP calls with the Azure OpenAI GPT Realtime API.
 *
 * Implements https://developers.openai.com/api/docs/guides/voice-sip?api=realtime
 * with the three differences Azure requires:
 *
 *   1. Base URL   https://<resource>.openai.azure.com/openai/v1
 *                 instead of https://api.openai.com/v1
 *   2. Auth       "api-key: <key>" instead of "Authorization: Bearer <key>"
 *   3. model      the name of your *deployment*, not the model name
 *
 * Call flow:
 *   SIP trunk -> Azure -> POST /webhook   (realtime.call.incoming)
 *                      -> POST /realtime/calls/{call_id}/accept
 *                      -> WSS  /realtime?call_id={call_id} -> response.create
 */
import "dotenv/config";
import crypto from "node:crypto";
import express, { type Request, type Response } from "express";
import WebSocket from "ws";
import OpenAI from "openai";

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

const missing: string[] = [];
const required = (name: string): string => {
  const value = process.env[name]?.trim();
  if (!value) missing.push(name);
  return value ?? "";
};

const ENDPOINT = required("AZURE_OPENAI_ENDPOINT").replace(/\/+$/, "");
const API_KEY = required("AZURE_OPENAI_API_KEY");
const DEPLOYMENT = required("AZURE_OPENAI_DEPLOYMENT");
const WEBHOOK_SECRET = required("AZURE_OPENAI_WEBHOOK_SECRET");

if (missing.length > 0) {
  console.error(
    `Missing required environment variable(s):\n${missing
      .map((name) => `  - ${name}`)
      .join("\n")}\n\nCopy .env.example to .env and fill these in.`
  );
  process.exit(1);
}

const PORT = Number(process.env.PORT ?? 8000);
const API_BASE = `${ENDPOINT}/openai/v1`;
const WS_BASE = API_BASE.replace(/^https:/, "wss:");
const AUTH_HEADER = { "api-key": API_KEY };

const ADMIN_TOKEN = process.env.ADMIN_API_TOKEN?.trim() || null;
const LOG_PAYLOADS =
  (process.env.LOG_EVENT_PAYLOADS ?? "true").toLowerCase() !== "false";

/** Accept body, as described in the guide. `model` is the deployment name. */
const callAccept = {
  type: "realtime",
  model: DEPLOYMENT,
  instructions:
    process.env.AGENT_INSTRUCTIONS?.trim() || "You are a support agent.",
  ...(process.env.AGENT_VOICE?.trim()
    ? { audio: { output: { voice: process.env.AGENT_VOICE.trim() } } }
    : {}),
};

/** Sent once the monitoring socket opens, as in the guide's example. */
const responseCreate = {
  type: "response.create",
  response: {
    instructions: `Say to the user: ${
      process.env.WELCOME_GREETING?.trim() ||
      "Thank you for calling, how can I help you?"
    }`,
  },
};

// ---------------------------------------------------------------------------
// Call control: accept / reject / refer / hangup
// ---------------------------------------------------------------------------

class CallActionError extends Error {
  constructor(
    readonly action: string,
    readonly status: number,
    readonly body: string
  ) {
    super(`${action} failed: ${status} ${body}`);
    this.name = "CallActionError";
  }
}

/** Renders JSON for logs, hiding long strings such as base64 audio deltas. */
const brief = (value: unknown): string =>
  JSON.stringify(value, (_key, inner: unknown) =>
    typeof inner === "string" && inner.length > 120
      ? `<${inner.length} chars omitted>`
      : inner
  );

const callAction = async (
  callId: string,
  action: string,
  body?: unknown
): Promise<void> => {
  const url = `${API_BASE}/realtime/calls/${encodeURIComponent(callId)}/${action}`;
  if (LOG_PAYLOADS) {
    console.log(`[${callId}] POST ${action} ${body ? brief(body) : "(no body)"}`);
  }

  const response = await fetch(url, {
    method: "POST",
    headers: {
      ...AUTH_HEADER,
      ...(body === undefined ? {} : { "Content-Type": "application/json" }),
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });

  const text = await response.text().catch(() => "");
  if (LOG_PAYLOADS) {
    console.log(`[${callId}] ${action} -> ${response.status} ${text || "(empty)"}`);
  }
  if (!response.ok) throw new CallActionError(action, response.status, text);
};

const acceptCall = (callId: string) => callAction(callId, "accept", callAccept);
const rejectCall = (callId: string, statusCode?: number) =>
  callAction(
    callId,
    "reject",
    statusCode === undefined ? {} : { status_code: statusCode }
  );
const referCall = (callId: string, targetUri: string) =>
  callAction(callId, "refer", { target_uri: targetUri });
const hangupCall = (callId: string) => callAction(callId, "hangup");

// ---------------------------------------------------------------------------
// Monitor the session over WebSocket
// ---------------------------------------------------------------------------

const monitorCall = (callId: string): void => {
  const url = `${WS_BASE}/realtime?call_id=${encodeURIComponent(callId)}`;
  const ws = new WebSocket(url, { headers: AUTH_HEADER });
  const t0 = Date.now();
  const at = (): string => `+${Date.now() - t0}ms`;

  ws.on("open", () => {
    console.log(`[${callId}] ${at()} websocket open`);
    console.log(`[${callId}] ${at()} -> ${brief(responseCreate)}`);
    ws.send(JSON.stringify(responseCreate));
  });

  ws.on("message", (data) => {
    const raw = data.toString();
    let event: { type?: string; response?: { status?: string } };
    try {
      event = JSON.parse(raw) as typeof event;
    } catch {
      console.log(`[${callId}] ${at()} <- non-JSON frame (${raw.length} bytes)`);
      return;
    }

    const failed =
      event.type === "error" ||
      (event.type === "response.done" && event.response?.status !== "completed");

    if (LOG_PAYLOADS) {
      console.log(`[${callId}] ${at()} ${failed ? "!!" : "<-"} ${brief(event)}`);
    } else if (failed) {
      console.error(`[${callId}] ${at()} !! ${brief(event)}`);
    } else {
      console.log(`[${callId}] ${at()} <- ${event.type ?? "unknown"}`);
    }
  });

  ws.on("error", (error) => {
    console.error(`[${callId}] ${at()} websocket error: ${error.message}`);
  });

  ws.on("close", (code, reason) => {
    console.log(
      `[${callId}] ${at()} websocket closed: ${code} ${reason.toString()}`
    );
  });
};

// ---------------------------------------------------------------------------
// Webhook server
// ---------------------------------------------------------------------------

/** Used only to verify signatures, so this key is never sent anywhere. */
const verifier = new OpenAI({
  apiKey: "unused-signature-verification-only",
  webhookSecret: WEBHOOK_SECRET,
});

const handleWebhook = async (req: Request, res: Response): Promise<void> => {
  let event: { type?: string; data?: { call_id?: string } };

  try {
    const payload = Buffer.isBuffer(req.body)
      ? req.body.toString("utf8")
      : String(req.body);
    event = (await verifier.webhooks.unwrap(
      payload,
      req.headers as Record<string, string>,
      WEBHOOK_SECRET
    )) as typeof event;
  } catch (error) {
    console.error("Invalid signature:", (error as Error).message);
    res.status(400).send("Invalid signature");
    return;
  }

  const callId = event?.data?.call_id;
  if (event?.type !== "realtime.call.incoming" || !callId) {
    console.log(`Ignoring event of type ${event?.type ?? "unknown"}`);
    res.sendStatus(200);
    return;
  }

  console.log(`[${callId}] incoming call`);

  try {
    await acceptCall(callId);
  } catch (error) {
    console.error(`[${callId}] ${(error as Error).message}`);
    const status = error instanceof CallActionError ? error.status : 500;
    res.status(502).json({ error: "accept failed", status });
    return;
  }

  monitorCall(callId);
  res.sendStatus(200);
};

const app = express();

// Signature verification needs the exact bytes Azure signed.
app.post("/webhook", express.raw({ type: "*/*" }), handleWebhook);
app.post("/", express.raw({ type: "*/*" }), handleWebhook);

app.use(express.json());

app.get("/health", (_req: Request, res: Response) => {
  res.json({ status: "ok", deployment: DEPLOYMENT });
});

/** Call control over HTTP. Disabled unless ADMIN_API_TOKEN is set. */
app.post("/calls/:callId/:action", async (req: Request, res: Response) => {
  const { callId, action } = req.params;

  if (!ADMIN_TOKEN) {
    res.status(404).send("Set ADMIN_API_TOKEN to enable call control");
    return;
  }
  const given = Buffer.from(req.header("x-admin-token") ?? "");
  const want = Buffer.from(ADMIN_TOKEN);
  if (given.length !== want.length || !crypto.timingSafeEqual(given, want)) {
    res.status(401).send("Unauthorized");
    return;
  }

  const body = (req.body ?? {}) as { status_code?: number; target_uri?: string };

  try {
    if (action === "reject") {
      await rejectCall(callId, body.status_code);
    } else if (action === "hangup") {
      await hangupCall(callId);
    } else if (action === "refer") {
      if (!body.target_uri) {
        res.status(400).json({ error: "target_uri is required" });
        return;
      }
      await referCall(callId, body.target_uri);
    } else {
      res.status(404).json({ error: `unknown action: ${action}` });
      return;
    }
    res.json({ call_id: callId, action, status: "ok" });
  } catch (error) {
    console.error(`[${callId}] ${(error as Error).message}`);
    const status = error instanceof CallActionError ? error.status : 500;
    res.status(502).json({ call_id: callId, action, status });
  }
});

app.listen(PORT, "0.0.0.0", () => {
  console.log(`Listening on http://0.0.0.0:${PORT}`);
  console.log(`Endpoint:    ${ENDPOINT}`);
  console.log(`Deployment:  ${DEPLOYMENT}`);
  console.log(`Accept body: ${JSON.stringify(callAccept)}`);
});
