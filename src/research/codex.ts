import { appendFile } from "node:fs/promises";
import { Codex } from "@openai/codex-sdk";
import type { Thread } from "@openai/codex-sdk";

export type ResearchThreadOptions = {
  workingDirectory: string;
  runId: number;
  exaProxyUrl: string;
};

export function createResearchThread(opts: ResearchThreadOptions): Thread {
  const codex = new Codex({
    config: {
      sandbox_workspace_write: { network_access: true },
      mcp_servers: {
        parts_research_proxy: {
          url: opts.exaProxyUrl,
          http_headers: { "X-Run-Id": String(opts.runId) },
          default_tools_approval_mode: "approve",
          startup_timeout_sec: 30,
          tool_timeout_sec: 120,
        },
      },
    },
  });

  return codex.startThread({
    workingDirectory: opts.workingDirectory,
    skipGitRepoCheck: true,
    sandboxMode: "workspace-write",
    approvalPolicy: "never",
    networkAccessEnabled: true,
    webSearchMode: "disabled",
  });
}

// Один turn через runStreamed: пишем события в jsonl и возвращаемся, когда
// поток закрывается. Сам факт того, что агент вызвал write_result, проверяем
// дальше — по содержимому файла на диске.
export async function runTurnStreamed(
  thread: Thread,
  prompt: string,
  messagesJsonlPath: string,
): Promise<void> {
  const result = await thread.runStreamed(prompt);
  for await (const event of result.events) {
    await appendFile(messagesJsonlPath, JSON.stringify(event) + "\n");
  }
}
