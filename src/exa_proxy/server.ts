import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { randomUUID } from "node:crypto";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { z } from "zod";
import { getCuratorSessionId, getRunId, runCtxStorage } from "./runContext.js";
import { cachedExaCall } from "./exaCached.js";
import { writeResultForRun } from "./writeResult.js";
import { executeSqlForCurator } from "../curator/tools/executeSql.js";
import { saveToSmart, type SaveOp } from "../curator/tools/saveToSmart.js";
import { markNeedsReview } from "../curator/tools/markNeedsReview.js";

export type ExaProxyOptions = {
  port: number;
  host?: string;
};

async function handleExaTool(toolName: string, args: Record<string, unknown>): Promise<unknown> {
  // У research-агента есть runId, у куратора — curatorSessionId. Cache usage
  // привязываем только к runId; для куратора hit/miss считается, но не пишется в exa_cache_usage.
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
        "Research-only. Validate and persist the final structured JSON for the current research run. Call this once you have collected all data; backend will validate and store it.",
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
          content: [{ type: "text", text: "ERROR: write_result is only available for research runs." }],
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

  server.registerTool(
    "execute_sql",
    {
      title: "Execute SQL (curator)",
      description:
        "Curator-only. Run raw SQL on parts_research (+ smart.* and brand_mapping.* via FDW). Returns rows for SELECT or row_count for write ops. Every call is logged in agent_sql_log.",
      inputSchema: {
        sql: z.string().describe("Raw SQL. Multiple statements via ';' allowed."),
      },
    },
    async (args) => {
      const sid = getCuratorSessionId();
      if (sid === null) {
        return {
          content: [{ type: "text", text: "ERROR: execute_sql is only available for curator sessions." }],
          isError: true,
        };
      }
      const outcome = await executeSqlForCurator(sid, args.sql);
      if (!outcome.ok) {
        return { content: [{ type: "text", text: `ERROR: ${outcome.error}` }], isError: true };
      }
      return {
        content: [
          {
            type: "text",
            text: JSON.stringify(
              { command: outcome.command, row_count: outcome.row_count, rows: outcome.rows },
              null,
              2,
            ),
          },
        ],
      };
    },
  );

  server.registerTool(
    "save_to_smart",
    {
      title: "Save to Smart (curator)",
      description:
        "Curator-only. Batch-publish operations to smart.* via FDW. Each op is INSERT with a per-row SAVEPOINT; a failed op doesn't break the rest. For every successful op a publications row is also written. Returns per-op {op_index, status, smart_id?, error?}.",
      inputSchema: {
        operations: z
          .array(
            z.object({
              table: z.enum(["parts", "part_brands", "part_components"]),
              action: z.literal("insert"),
              fields: z.record(z.string(), z.unknown()),
              run_id: z.number().int(),
            }),
          )
          .min(1),
      },
    },
    async (args) => {
      const sid = getCuratorSessionId();
      if (sid === null) {
        return {
          content: [{ type: "text", text: "ERROR: save_to_smart is only available for curator sessions." }],
          isError: true,
        };
      }
      const results = await saveToSmart(sid, args.operations as SaveOp[]);
      return {
        content: [{ type: "text", text: JSON.stringify(results, null, 2) }],
      };
    },
  );

  server.registerTool(
    "mark_needs_review",
    {
      title: "Mark Needs Review (curator)",
      description:
        "Curator-only. Set task_run.status='needs_human_review' with the given reason. Use when data is collected but cannot be auto-published.",
      inputSchema: {
        run_id: z.number().int(),
        reason: z.string().min(1),
      },
    },
    async (args) => {
      const sid = getCuratorSessionId();
      if (sid === null) {
        return {
          content: [{ type: "text", text: "ERROR: mark_needs_review is only available for curator sessions." }],
          isError: true,
        };
      }
      const outcome = await markNeedsReview(args.run_id, args.reason);
      if (!outcome.ok) {
        return { content: [{ type: "text", text: `ERROR: ${outcome.error}` }], isError: true };
      }
      return { content: [{ type: "text", text: `OK: run ${args.run_id} marked as needs_human_review.` }] };
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
    const curatorHeader = req.headers["x-curator-session-id"];
    const runId =
      typeof runIdHeader === "string" && /^\d+$/.test(runIdHeader)
        ? parseInt(runIdHeader, 10)
        : null;
    const curatorSessionId =
      typeof curatorHeader === "string" && /^\d+$/.test(curatorHeader)
        ? parseInt(curatorHeader, 10)
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
    await runCtxStorage.run({ runId, curatorSessionId }, async () => {
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
