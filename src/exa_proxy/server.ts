import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { randomUUID } from "node:crypto";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { z } from "zod";
import { getRunId, runCtxStorage } from "./runContext.js";
import { cachedExaCall } from "./exaCached.js";
import { writeResultForRun } from "./writeResult.js";

export type ExaProxyOptions = {
  port: number;
  host?: string;
};

async function handleExaTool(toolName: string, args: Record<string, unknown>): Promise<unknown> {
  return cachedExaCall(toolName, args, getRunId());
}

function normalizeContent(
  result: unknown,
): Array<{ type: "text"; text: string }> {
  if (
    result &&
    typeof result === "object" &&
    "content" in result &&
    Array.isArray((result as { content: unknown }).content)
  ) {
    const items = (result as { content: unknown[] }).content;
    const out: Array<{ type: "text"; text: string }> = [];
    for (const raw of items) {
      const item = raw as Record<string, unknown>;
      if (item && typeof item === "object" && item.type === "text" && typeof item.text === "string") {
        out.push({ type: "text", text: item.text });
      }
    }
    if (out.length > 0) return out;
  }
  return [{ type: "text", text: JSON.stringify(result) }];
}

function buildMcpServer(): McpServer {
  const server = new McpServer({ name: "parts-research-proxy", version: "0.1.0" });

  // Сохраняем оригинальные описания и схемы Exa-инструментов, чтобы агент видел
  // привычный интерфейс и не догадывался про прокси.
  server.registerTool(
    "web_search_exa",
    {
      title: "Web Search Exa",
      description:
        "Search the web for any topic and get clean, ready-to-use content. Returns clean text content from top search results.",
      inputSchema: {
        query: z.string().describe("Natural language search query."),
        numResults: z
          .number()
          .int()
          .min(1)
          .max(100)
          .optional()
          .describe("Number of results (default 10)."),
      },
    },
    async (args) => {
      const result = await handleExaTool("web_search_exa", args);
      return { content: normalizeContent(result) };
    },
  );

  server.registerTool(
    "web_fetch_exa",
    {
      title: "Web Fetch Exa",
      description:
        "Read a webpage's full content as clean markdown. Use after web_search_exa when highlights are insufficient.",
      inputSchema: {
        urls: z.array(z.string()).describe("URLs to fetch."),
        maxCharacters: z
          .number()
          .int()
          .min(1)
          .optional()
          .describe("Max characters per page (default 3000)."),
      },
    },
    async (args) => {
      const result = await handleExaTool("web_fetch_exa", args);
      return { content: normalizeContent(result) };
    },
  );

  server.registerTool(
    "write_result",
    {
      title: "Write Result",
      description:
        "Validate and persist the final structured JSON for the current research run. Call this once you have collected all data; backend will validate and store it.",
      inputSchema: {
        json: z
          .unknown()
          .describe("Final structured JSON for the task. Must match the schema in the prompt."),
        task_part_number: z
          .string()
          .describe("Task part number, exactly as given in the prompt. Used for validation."),
      },
    },
    async (args) => {
      const runId = getRunId();
      if (runId === null) {
        return {
          content: [{ type: "text", text: "ERROR: missing X-Run-Id header on this MCP request." }],
          isError: true,
        };
      }
      const outcome = await writeResultForRun(runId, args.task_part_number, args.json);
      if (!outcome.ok) {
        return {
          content: [{ type: "text", text: `ERROR: ${outcome.error}` }],
          isError: true,
        };
      }
      return {
        content: [
          {
            type: "text",
            text: `OK: result saved for run ${runId}. End your turn now.`,
          },
        ],
      };
    },
  );

  return server;
}

export async function startExaProxy(opts: ExaProxyOptions): Promise<{ close: () => Promise<void> }> {
  const host = opts.host ?? "127.0.0.1";

  // Один транспорт на процесс — stateful, JSON-режим (без SSE).
  const transports = new Map<string, StreamableHTTPServerTransport>();
  const servers = new Map<string, McpServer>();

  async function getOrCreateSession(sessionId: string | undefined): Promise<{
    transport: StreamableHTTPServerTransport;
  }> {
    if (sessionId && transports.has(sessionId)) {
      return { transport: transports.get(sessionId)! };
    }
    const newId = sessionId ?? randomUUID();
    const transport = new StreamableHTTPServerTransport({
      sessionIdGenerator: () => newId,
      enableJsonResponse: true,
    });
    const server = buildMcpServer();
    await server.connect(transport);
    transports.set(newId, transport);
    servers.set(newId, server);
    transport.onclose = () => {
      transports.delete(newId);
      servers.delete(newId);
    };
    return { transport };
  }

  async function handle(req: IncomingMessage, res: ServerResponse) {
    const sessionId = (req.headers["mcp-session-id"] as string | undefined) ?? undefined;
    const runIdHeader = req.headers["x-run-id"];
    const runId =
      typeof runIdHeader === "string" && /^\d+$/.test(runIdHeader)
        ? parseInt(runIdHeader, 10)
        : null;

    let body: unknown;
    if (req.method === "POST") {
      const chunks: Buffer[] = [];
      for await (const c of req) chunks.push(c as Buffer);
      if (chunks.length) {
        try {
          body = JSON.parse(Buffer.concat(chunks).toString("utf8"));
        } catch {
          body = undefined;
        }
      }
    }

    const { transport } = await getOrCreateSession(sessionId);
    await runCtxStorage.run({ runId }, async () => {
      try {
        await transport.handleRequest(req, res, body);
      } catch (err) {
        console.error("[exa-proxy] handleRequest error:", err);
        if (!res.headersSent) {
          res.statusCode = 500;
          res.end(JSON.stringify({ error: String(err) }));
        }
      }
    });
  }

  const httpServer = createServer((req, res) => {
    void handle(req, res);
  });

  await new Promise<void>((r) => httpServer.listen(opts.port, host, () => r()));
  console.log(`[exa-proxy] listening on http://${host}:${opts.port}/`);

  return {
    close: async () => {
      await new Promise<void>((r) => httpServer.close(() => r()));
      for (const t of transports.values()) await t.close().catch(() => {});
    },
  };
}
