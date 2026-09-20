/**
 * Posts a correctly signed `realtime.call.incoming` event at the local server so
 * the webhook and accept path can be exercised without placing a phone call.
 *
 * Signing follows the Standard Webhooks scheme Azure OpenAI uses:
 *   base64(HMAC-SHA256(secret, "{webhook-id}.{timestamp}.{body}"))
 *
 *   npm run webhook:test
 *   npx tsx scripts/send-test-webhook.ts <url> [call_id]
 */
import "dotenv/config";
import crypto from "node:crypto";

const SECRET = process.env.AZURE_OPENAI_WEBHOOK_SECRET?.trim();
if (!SECRET) {
  console.error("AZURE_OPENAI_WEBHOOK_SECRET must be set in .env");
  process.exit(1);
}

const port = process.env.PORT ?? "8000";
const target = process.argv[2] ?? `http://127.0.0.1:${port}/webhook`;
const callId = process.argv[3] ?? `rtc_test_${crypto.randomUUID().replace(/-/g, "")}`;

const payload = JSON.stringify({
  object: "event",
  id: `evt_${crypto.randomUUID().replace(/-/g, "")}`,
  type: "realtime.call.incoming",
  created_at: Math.floor(Date.now() / 1000),
  data: {
    call_id: callId,
    sip_headers: [
      { name: "From", value: "sip:+14255551212@sip.example.com" },
      { name: "To", value: "sip:+18005551212@sip.example.com" },
      { name: "Call-ID", value: callId },
    ],
  },
});

const webhookId = `wh_${crypto.randomUUID().replace(/-/g, "")}`;
const timestamp = Math.floor(Date.now() / 1000).toString();
const key = SECRET.startsWith("whsec_")
  ? Buffer.from(SECRET.slice("whsec_".length), "base64")
  : Buffer.from(SECRET, "utf-8");

const signature = crypto
  .createHmac("sha256", key)
  .update(`${webhookId}.${timestamp}.${payload}`)
  .digest("base64");

console.log(`POST ${target}\n  call_id: ${callId}`);

const response = await fetch(target, {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    "webhook-id": webhookId,
    "webhook-timestamp": timestamp,
    "webhook-signature": `v1,${signature}`,
  },
  body: payload,
}).catch((error: Error) => {
  console.error(
    `  -> could not reach ${target}\n     ${error.cause ?? error.message}\n\n` +
      "Start the server first with `npm run dev`."
  );
  process.exit(1);
});

const body = await response.text();
console.log(`  -> ${response.status} ${response.statusText} ${body}`);

let parsed: { status?: number } = {};
try {
  parsed = JSON.parse(body) as typeof parsed;
} catch {
  // Plain-text response such as "Invalid signature".
}

if (response.status === 400) {
  console.error(
    "\nFAIL: the signature was rejected. AZURE_OPENAI_WEBHOOK_SECRET does not match\n" +
      "the value the running server loaded. Restart the server after editing .env."
  );
  process.exit(1);
}

if (parsed.status === 404 || body.includes("call_id_not_found")) {
  console.log(
    "\nPASS: signature verified and the accept request reached Azure OpenAI.\n\n" +
      "Azure replied call_id_not_found because this event carries a synthetic call_id\n" +
      "with no live SIP session behind it. That is the expected result here, and it\n" +
      "confirms the webhook secret, endpoint URL and API key are all correct.\n" +
      "(A bad key would return 401, not 404.)"
  );
  process.exit(0);
}

if (!response.ok) {
  console.error("\nFAIL: see the server log for the response from Azure OpenAI.");
  process.exit(1);
}

console.log("\nPASS: the webhook was accepted end to end.");
