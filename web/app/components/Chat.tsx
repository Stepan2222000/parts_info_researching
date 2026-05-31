"use client";

import { useChat } from "@ai-sdk/react";
import { DefaultChatTransport, type UIMessage } from "ai";
import { useMemo, useRef, useState, useEffect } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { IconArrowUp, IconDatabase, Sparkle } from "@/lib/icons";

const SUGGESTIONS = [
  "Сколько done-ранов ждут публикации?",
  "Покажи draft по последнему готовому run.",
  "Обработай очередь.",
];

function lastUserText(messages: UIMessage[]): string {
  for (let i = messages.length - 1; i >= 0; i--) {
    if (messages[i].role === "user") {
      return messages[i].parts
        .filter((p) => p.type === "text")
        .map((p) => (p as { text: string }).text)
        .join("\n");
    }
  }
  return "";
}

function Markdown({ text }: { text: string }) {
  return (
    <div className="prose">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
    </div>
  );
}

function ToolCall({ part }: { part: any }) {
  const name: string = part.toolName ?? (part.type?.startsWith("tool-") ? part.type.slice(5) : "tool");
  const done = part.state === "output-available" || part.state === "output-error";
  const [open, setOpen] = useState(false);
  const input = part.input ?? part.args;
  const output = part.output ?? part.result ?? part.errorText;
  return (
    <div className="tool">
      <div className="tool-head" onClick={() => setOpen((o) => !o)}>
        <IconDatabase />
        <span className="tname">{name}</span>
        <span className="state">
          {done ? <span style={{ color: "var(--success)" }}>готово</span> : <span className="spin" />}
        </span>
      </div>
      {open && (
        <div className="tool-body">
          {input != null && (
            <>
              <div className="lbl">Вход</div>
              <pre>{typeof input === "string" ? input : JSON.stringify(input, null, 2)}</pre>
            </>
          )}
          {output != null && (
            <>
              <div className="lbl">Результат</div>
              <pre>{typeof output === "string" ? output : JSON.stringify(output, null, 2)}</pre>
            </>
          )}
        </div>
      )}
    </div>
  );
}

function isToolPart(p: any) {
  return p.type === "dynamic-tool" || (typeof p.type === "string" && p.type.startsWith("tool-"));
}

function AssistantMessage({ m }: { m: UIMessage }) {
  return (
    <div className="msg-assistant">
      <div className="who"><span className="mark"><Sparkle width={14} height={14} /></span> Куратор</div>
      {m.parts.map((p: any, i: number) => {
        if (p.type === "text") return <Markdown key={i} text={p.text} />;
        if (isToolPart(p)) return <ToolCall key={i} part={p} />;
        return null;
      })}
    </div>
  );
}

export function Chat({
  sessionId,
  initialMessages,
  onActivity,
}: {
  sessionId: number;
  initialMessages: UIMessage[];
  onActivity?: () => void;
}) {
  const [input, setInput] = useState("");
  const transport = useMemo(
    () =>
      new DefaultChatTransport({
        api: "/api/curator/chat",
        prepareSendMessagesRequest: ({ messages }) => ({
          body: { session_id: sessionId, text: lastUserText(messages) },
        }),
      }),
    [sessionId]
  );

  const { messages, sendMessage, status, error } = useChat({ messages: initialMessages, transport });
  const busy = status === "submitted" || status === "streaming";
  const threadRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    threadRef.current?.scrollTo({ top: threadRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, busy]);

  function submit() {
    const t = input.trim();
    if (!t || busy) return;
    sendMessage({ text: t });
    setInput("");
    onActivity?.();
  }

  const lastIsUser = messages.length > 0 && messages[messages.length - 1].role === "user";

  return (
    <div className="chat">
      <div className="thread" ref={threadRef}>
        <div className="thread-inner">
          {messages.length === 0 && (
            <div style={{ paddingTop: "8vh" }}>
              <div className="empty" style={{ padding: 0, textAlign: "left" }}>
                <div className="serif" style={{ display: "flex", alignItems: "center", gap: 12 }}>
                  <span style={{ color: "var(--brand-coral)" }}><Sparkle width={26} height={26} /></span>
                  Чем помочь с каталогом?
                </div>
                <div style={{ color: "var(--on-dark-soft)", marginTop: 6 }}>
                  Куратор видит draft-данные, evidence и Smart. Попроси обработать очередь — он сам решит, что публиковать.
                </div>
              </div>
              <div className="suggestions">
                {SUGGESTIONS.map((s) => (
                  <button key={s} className="suggestion" onClick={() => sendMessage({ text: s })}>{s}</button>
                ))}
              </div>
            </div>
          )}

          {messages.map((m) =>
            m.role === "user" ? (
              <div className="msg-user" key={m.id}>
                {m.parts.filter((p) => p.type === "text").map((p, i) => (
                  <span key={i}>{(p as { text: string }).text}</span>
                ))}
              </div>
            ) : (
              <AssistantMessage key={m.id} m={m} />
            )
          )}

          {busy && lastIsUser && (
            <div className="msg-assistant">
              <div className="who"><span className="mark"><Sparkle width={14} height={14} /></span> Куратор</div>
              <div className="typing"><span /><span /><span /></div>
            </div>
          )}

          {status === "error" && (
            <div className="banner error">Ошибка: {error?.message ?? "не удалось получить ответ"}</div>
          )}
        </div>
      </div>

      <div className="composer-wrap">
        <div className="composer">
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="Напиши куратору…  (Enter — отправить, Shift+Enter — перенос строки)"
            rows={1}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                submit();
              }
            }}
          />
          <div className="composer-actions">
            <span className="muted">сессия {sessionId}</span>
            <button className="send-btn" onClick={submit} disabled={!input.trim() || busy} aria-label="Отправить">
              <IconArrowUp />
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
