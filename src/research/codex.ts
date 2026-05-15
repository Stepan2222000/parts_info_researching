import { appendFile } from "node:fs/promises";
import { Codex } from "@openai/codex-sdk";
import type { Thread } from "@openai/codex-sdk";

export function createResearchThread(workingDirectory: string): Thread {
  const codex = new Codex();
  return codex.startThread({
    workingDirectory,
    skipGitRepoCheck: true,
    sandboxMode: "workspace-write",
    approvalPolicy: "never",
    networkAccessEnabled: false,
    webSearchMode: "disabled",
  });
}

// Прогоняет один turn через runStreamed и пишет все события в jsonl.
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
