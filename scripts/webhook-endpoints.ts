/**
 * Manage Azure OpenAI webhook endpoints.
 *
 * Azure has no portal UI for these, so registration is REST-only:
 *   POST/GET/DELETE {endpoint}/openai/v1/dashboard/webhook_endpoints
 *
 *   npm run webhook:create           register WEBHOOK_PUBLIC_URL
 *   npm run webhook:list             list registered endpoints
 *   npm run webhook:delete -- <id>   delete one
 */
import "dotenv/config";

const ENDPOINT = process.env.AZURE_OPENAI_ENDPOINT?.trim().replace(/\/+$/, "");
const API_KEY = process.env.AZURE_OPENAI_API_KEY?.trim();

if (!ENDPOINT || !API_KEY) {
  console.error("AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY must be set in .env");
  process.exit(1);
}

const URL_BASE = `${ENDPOINT}/openai/v1/dashboard/webhook_endpoints`;

const request = async (
  method: string,
  url: string,
  body?: unknown
): Promise<unknown> => {
  let response: Response;
  try {
    response = await fetch(url, {
      method,
      headers: {
        "api-key": API_KEY,
        Accept: "application/json",
        ...(body === undefined ? {} : { "Content-Type": "application/json" }),
      },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
  } catch (error) {
    console.error(
      `Could not reach ${url}\n  ${(error as Error).cause ?? (error as Error).message}\n\n` +
        "Check that AZURE_OPENAI_ENDPOINT points at your Azure OpenAI resource."
    );
    process.exit(1);
  }

  const text = await response.text();
  if (!response.ok) {
    console.error(`${method} ${url} -> ${response.status}\n${text}`);
    process.exit(1);
  }
  return text ? JSON.parse(text) : null;
};

const create = async (): Promise<void> => {
  const url = process.env.WEBHOOK_PUBLIC_URL?.trim();
  if (!url?.startsWith("https://")) {
    console.error(
      "WEBHOOK_PUBLIC_URL must be set to this server's public https webhook route,\n" +
        "for example https://<tunnel-id>-8000.<cluster>.devtunnels.ms/webhook"
    );
    process.exit(1);
  }

  const result = await request("POST", URL_BASE, {
    name: process.env.WEBHOOK_NAME?.trim() || "sip-realtime-listener",
    url,
    event_types: ["realtime.call.incoming"],
  });

  console.log(JSON.stringify(result, null, 2));
  console.log(
    "\n" +
      "-".repeat(72) +
      "\nCopy signing_secret above into .env as AZURE_OPENAI_WEBHOOK_SECRET." +
      "\nIt is shown only once and cannot be retrieved again.\n" +
      "-".repeat(72)
  );
};

const [command, ...args] = process.argv.slice(2);

switch (command) {
  case "create":
    await create();
    break;
  case "list":
    console.log(JSON.stringify(await request("GET", URL_BASE), null, 2));
    break;
  case "delete": {
    const id = args[0];
    if (!id) {
      console.error("Usage: npm run webhook:delete -- <webhook_endpoint_id>");
      process.exit(1);
    }
    await request("DELETE", `${URL_BASE}/${encodeURIComponent(id)}`);
    console.log(`Deleted webhook endpoint ${id}`);
    break;
  }
  default:
    console.error(
      [
        "Usage:",
        "  npm run webhook:create           register WEBHOOK_PUBLIC_URL",
        "  npm run webhook:list             list registered endpoints",
        "  npm run webhook:delete -- <id>   delete one",
      ].join("\n")
    );
    process.exit(1);
}
