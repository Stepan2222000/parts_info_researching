"use client";

import { useCallback, useEffect, useState } from "react";
import type { UIMessage } from "ai";
import { createSession, getQueue, getSessions, type QueueData, type SessionSummary } from "@/lib/api";
import { Rail } from "@/components/Rail";
import { Chat } from "@/components/Chat";
import { BacklogChips } from "@/components/BacklogChips";
import { IconNewChat } from "@/lib/icons";

interface ApiMessage {
  id: number;
  role: "user" | "assistant" | "tool";
  content: string | null;
  tool_call: { tool?: string; arguments?: string; output?: unknown } | null;
}

function parseMaybe(s: string | undefined): unknown {
  if (!s) return {};
  try { return JSON.parse(s); } catch { return s; }
}

// curator_messages → AI SDK UIMessage[] (для просмотра/продолжения прошлой сессии).
function toUIMessages(rows: ApiMessage[]): UIMessage[] {
  return rows.map((r) => {
    if (r.role === "tool" && r.tool_call) {
      return {
        id: `t${r.id}`,
        role: "assistant",
        parts: [
          {
            type: "dynamic-tool",
            toolName: r.tool_call.tool ?? "tool",
            toolCallId: `c${r.id}`,
            state: "output-available",
            input: parseMaybe(r.tool_call.arguments),
            output: r.tool_call.output,
          } as unknown as UIMessage["parts"][number],
        ],
      };
    }
    return {
      id: `${r.role[0]}${r.id}`,
      role: r.role === "user" ? "user" : "assistant",
      parts: [{ type: "text", text: r.content ?? "" }],
    };
  });
}

export default function CuratorPage() {
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [active, setActive] = useState<{ id: number; msgs: UIMessage[] } | null>(null);
  // Очередь для чипов-бэклогов (воркер + куратор) в шапке.
  const [queue, setQueue] = useState<QueueData | null>(null);

  const refreshSessions = useCallback(async () => {
    try { setSessions((await getSessions()).sessions); } catch { /* surfaced elsewhere */ }
  }, []);

  // Не плодим пустые сессии: если есть пустая (0 сообщений) — переиспользуем самую свежую,
  // иначе создаём новую.
  const openOrCreate = useCallback(async () => {
    const ss = (await getSessions()).sessions;
    setSessions(ss);
    const empty = [...ss].sort((a, b) => b.session_id - a.session_id).find((s) => s.message_count === 0);
    if (empty) {
      setActive({ id: empty.session_id, msgs: [] });
      return;
    }
    const id = await createSession();
    setActive({ id, msgs: [] });
    setSessions((await getSessions()).sessions);
  }, []);

  const loadSession = useCallback(async (id: number) => {
    const r = await fetch(`/api/curator/sessions/${id}`, { cache: "no-store" });
    const data = await r.json();
    setActive({ id, msgs: toUIMessages(data.messages) });
  }, []);

  useEffect(() => {
    openOrCreate();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Поллим бэклог куратора (done без публикаций) для индикатора в шапке.
  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try { const q = await getQueue(); if (alive) setQueue(q); } catch { /* surfaced on dashboard */ }
    };
    tick();
    const t = setInterval(tick, 5000);
    return () => { alive = false; clearInterval(t); };
  }, []);

  return (
    <div className="app">
      <Rail>
        <button className="btn-ghost" style={{ margin: "8px 4px 4px", width: "calc(100% - 8px)" }} onClick={openOrCreate}>
          <IconNewChat /> Новый чат
        </button>
        <div className="rail-sep" />
        <div className="rail-label">Сессии</div>
        <div style={{ display: "flex", flexDirection: "column", gap: 2, overflowY: "auto", flex: 1, minHeight: 0 }}>
          {sessions.map((s) => (
            <button
              key={s.session_id}
              className={`session-item ${active?.id === s.session_id ? "active" : ""}`}
              onClick={() => loadSession(s.session_id)}
            >
              <span className="st">Сессия {s.session_id}</span>
              <span className="sm">
                {s.message_count} сообщений{s.ended_at ? " · завершена" : ""}
              </span>
            </button>
          ))}
          {sessions.length === 0 && <span className="muted" style={{ padding: "6px 11px" }}>Пока нет сессий.</span>}
        </div>
      </Rail>

      <div className="main">
        <div className="topbar">
          <div>
            <h1>Куратор</h1>
            <span className="sub">Публикует draft в Smart по твоему запросу.</span>
          </div>
          <BacklogChips data={queue} />
        </div>
        {active ? (
          <Chat key={active.id} sessionId={active.id} initialMessages={active.msgs} onActivity={refreshSessions} />
        ) : (
          <div className="empty"><div className="muted">Создаю сессию…</div></div>
        )}
      </div>
    </div>
  );
}
