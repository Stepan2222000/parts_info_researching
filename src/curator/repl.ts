import { mkdir, readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { createInterface } from "node:readline/promises";
import type { Thread } from "@openai/codex-sdk";
import { CURATOR_RULES_PATH, STORAGE_ROOT } from "../config.js";
import {
  countMessages,
  createSession,
  endSession,
  insertMessage,
  setSessionThreadId,
} from "../db/curator.js";
import { createCuratorThread } from "./thread.js";
import { formatSnapshot, loadSnapshot } from "./snapshot.js";

type SessionState = { id: number; thread: Thread; workingDir: string };

async function openSession(): Promise<SessionState> {
  const id = await createSession("");
  const workingDir = resolve(STORAGE_ROOT, "curator_sessions", String(id));
  await mkdir(workingDir, { recursive: true });
  const thread = createCuratorThread({ workingDirectory: workingDir, curatorSessionId: id });
  return { id, thread, workingDir };
}

async function buildPrompt(sessionId: number, userText: string, rules: string): Promise<string> {
  const isFirst = (await countMessages(sessionId)) === 0;
  const snapshot = formatSnapshot(await loadSnapshot());
  if (isFirst) {
    return `<rules>\n${rules}\n</rules>\n\n${snapshot}\n\n${userText}`;
  }
  return `${snapshot}\n\n${userText}`;
}

async function runTurn(state: SessionState, rules: string, userText: string): Promise<void> {
  await insertMessage(state.id, "user", userText, null);

  const prompt = await buildPrompt(state.id, userText, rules);
  process.stdout.write("\n");
  const result = await state.thread.runStreamed(prompt);

  let assistantText = "";

  for await (const ev of result.events) {
    if (ev.type === "thread.started") {
      await setSessionThreadId(state.id, ev.thread_id);
    } else if (ev.type === "item.started") {
      const it = ev.item;
      if (it.type === "mcp_tool_call") {
        console.log(`[tool ▶] ${it.tool}  args=${shortJson(it.arguments)}`);
      }
    } else if (ev.type === "item.completed") {
      const it = ev.item;
      if (it.type === "agent_message") {
        assistantText = it.text;
        process.stdout.write(`\n${it.text}\n`);
      } else if (it.type === "mcp_tool_call") {
        const tag = it.status === "completed" ? "✓" : it.status === "failed" ? "✗" : "?";
        const err = it.error ? ` err=${it.error.message}` : "";
        console.log(`[tool ${tag}] ${it.tool}${err}`);
        await insertMessage(state.id, "tool", null, it);
      } else if (it.type === "reasoning") {
        // тихо
      }
    } else if (ev.type === "turn.failed") {
      console.error(`[turn failed] ${ev.error.message}`);
    } else if (ev.type === "error") {
      console.error(`[fatal] ${ev.message}`);
    }
  }

  if (assistantText) {
    await insertMessage(state.id, "assistant", assistantText, null);
  }
}

function shortJson(v: unknown): string {
  const s = JSON.stringify(v);
  if (!s) return "";
  return s.length > 200 ? s.slice(0, 200) + "…" : s;
}

export async function runRepl(): Promise<void> {
  const rules = await readFile(CURATOR_RULES_PATH, "utf8");
  let state = await openSession();
  console.log(`[curator] session=${state.id} thread starting (model from env), cwd=${state.workingDir}`);
  console.log("Команды: /exit, /new. Пустая строка игнорируется.\n");

  const rl = createInterface({ input: process.stdin, output: process.stdout });
  let stdinClosed = false;
  rl.on("close", () => {
    stdinClosed = true;
  });
  try {
    for (;;) {
      if (stdinClosed) break;
      let line: string;
      try {
        line = (await rl.question("you ▸ ")).trim();
      } catch {
        break;
      }
      if (line === "") continue;
      if (line === "/exit") break;
      if (line === "/new") {
        await endSession(state.id);
        state = await openSession();
        console.log(`[curator] new session=${state.id}\n`);
        continue;
      }
      try {
        await runTurn(state, rules, line);
      } catch (err) {
        console.error("[turn error]", err instanceof Error ? err.message : err);
      }
    }
  } finally {
    rl.close();
    await endSession(state.id);
  }
}
