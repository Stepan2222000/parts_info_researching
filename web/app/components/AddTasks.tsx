"use client";

import { useState } from "react";
import { parseArticles, submitArticles, type SubmitResult } from "@/lib/api";
import { IconPlus } from "@/lib/icons";

const FB_LABEL: Record<string, string> = {
  queued: "в очередь",
  invalid: "невалиден",
  refused: "уже в Smart",
};

// Пресеты профиля этапов: применяются ко ВСЕМУ отправляемому списку.
const PRESETS: { value: string; label: string; hint: string }[] = [
  { value: "default", label: "Стандарт", hint: "все этапы, кроме свободного добора (phase2); ~5–7 мин" },
  { value: "full", label: "Полный", hint: "все этапы, включая свободный добор (phase2); ~8–10 мин" },
  { value: "fast", label: "Быстрый", hint: "только основной поиск и состав набора; ~2–3 мин" },
];

export function AddTasks({ onSubmitted }: { onSubmitted: () => void }) {
  const [raw, setRaw] = useState("");
  const [preset, setPreset] = useState("default");
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState<SubmitResult[]>([]);

  const articles = parseArticles(raw);

  async function submit() {
    if (!articles.length || busy) return;
    setBusy(true);
    try {
      const res = await submitArticles(articles, preset);
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
      <div className="profile-seg" role="radiogroup" aria-label="Профиль этапов">
        {PRESETS.map((p) => (
          <button
            type="button"
            key={p.value}
            className={`profile-opt${preset === p.value ? " active" : ""}`}
            title={p.hint}
            aria-pressed={preset === p.value}
            onClick={() => setPreset(p.value)}
          >
            {p.label}
          </button>
        ))}
      </div>
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
