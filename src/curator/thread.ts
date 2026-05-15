import { Codex } from "@openai/codex-sdk";
import type { Thread } from "@openai/codex-sdk";
import {
  CURATOR_MODEL,
  CURATOR_REASONING_EFFORT,
  EXA_PROXY_URL,
} from "../config.js";

export type CuratorThreadOptions = {
  workingDirectory: string;
  curatorSessionId: number;
};

export function createCuratorThread(opts: CuratorThreadOptions): Thread {
  const codex = new Codex({
    config: {
      sandbox_workspace_write: { network_access: true },
      mcp_servers: {
        parts_research_proxy: {
          url: EXA_PROXY_URL,
          http_headers: { "X-Curator-Session-Id": String(opts.curatorSessionId) },
          default_tools_approval_mode: "approve",
          startup_timeout_sec: 30,
          tool_timeout_sec: 180,
        },
      },
    },
  });

  return codex.startThread({
    model: CURATOR_MODEL,
    modelReasoningEffort: CURATOR_REASONING_EFFORT,
    workingDirectory: opts.workingDirectory,
    skipGitRepoCheck: true,
    sandboxMode: "workspace-write",
    approvalPolicy: "never",
    networkAccessEnabled: true,
    webSearchMode: "disabled",
  });
}
