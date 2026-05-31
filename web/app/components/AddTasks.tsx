"use client";

import { useState } from "react";
import { parseArticles, submitArticles, type SubmitResult } from "@/lib/api";
import { IconPlus } from "@/lib/icons";

const FB_LABEL: Record<string, string> = {
  queued: "в очередь",
  invalid: "невалиден",
  refused: "уже в Smart",
};

export function AddTasks({ onSubmitted }: { onSubmitted: () => void }) {
  const [raw, setRaw] = useState("");
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState<SubmitResult[]>([]);

  const articles = parseArticles(raw);

  async function submit() {
    if (!articles.length || busy) return;
    setBusy(true);
    try {
      const res = await submitArticles(articles);
      setFeedback(res.results);
      setRaw("");
      onSubmitted();
    } catch (e) {
      setFeedback([{ article: "—", status: "invalid", error: String(e), task_id: null, run_id: null, reused: false }]);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="add-box">
      <textarea
        value={raw}
        onChange={(e) => setRaw(e.target.value)}
        placeholder="Артикулы через пробел или с новой строки. Например: 807252T5 295100923 76868A04"
        onKeyDown={(e) => {
          if ((e.metaKey || e.ctrlKey) && e.key === "Enter") submit();
        }}
      />
      <button className="btn-primary" onClick={submit} disabled={!articles.length || busy}>
        <IconPlus />
        {busy ? "Отправляю…" : articles.length ? `Добавить ${articles.length}` : "Добавить в очередь"}
      </button>
      {feedback.length > 0 && (
        <div className="feedback">
          {feedback.map((f, i) => (
            <div className="feedback-row" key={i} title={f.error ?? ""}>
              <span className="art">{f.article}</span>
              <span className="msg">{FB_LABEL[f.status] ?? f.status}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
