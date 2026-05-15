import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { EXA_API_KEY } from "../config.js";

const EXA_TOOLS = ["web_search_exa"].join(",");
const EXA_URL = new URL(`https://mcp.exa.ai/mcp?tools=${EXA_TOOLS}`);
const NUM_RESULTS = 10;

export const EXA_NUM_RESULTS = NUM_RESULTS;

export async function callExaSearch(query: string): Promise<unknown> {
  const client = new Client({ name: "part-research-agent", version: "1.0.0" });
  const transport = new StreamableHTTPClientTransport(EXA_URL, {
    requestInit: { headers: { "x-api-key": EXA_API_KEY } },
  });

  await client.connect(transport);
  try {
    return await client.callTool({
      name: "web_search_exa",
      arguments: { query, numResults: NUM_RESULTS },
    });
  } finally {
    await transport.close();
  }
}

export function exaResultContainsArticle(result: unknown, article: string): boolean {
  return JSON.stringify(result).toLowerCase().includes(article.toLowerCase());
}
