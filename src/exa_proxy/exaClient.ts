import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { EXA_API_KEY } from "../config.js";

const EXA_URL = new URL("https://mcp.exa.ai/mcp");

// Простое per-call открытие клиента — overhead небольшой, нет состояния.
export async function callExaTool(toolName: string, args: Record<string, unknown>): Promise<unknown> {
  const client = new Client({ name: "parts-research-proxy", version: "0.1.0" });
  const transport = new StreamableHTTPClientTransport(EXA_URL, {
    requestInit: { headers: { "x-api-key": EXA_API_KEY } },
  });
  await client.connect(transport);
  try {
    return await client.callTool({ name: toolName, arguments: args });
  } finally {
    await transport.close();
  }
}
