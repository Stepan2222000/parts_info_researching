"use client";

import { useEffect, useState } from "react";
import { getRun, isTerminal, STATUS_LABEL, type ArticleRow, type RunDetail as Detail } from "@/lib/api";
import { StatusBadge } from "./StatusBadge";
import { IconClose, IconRefresh } from "@/lib/icons";

const CONF_LABEL: Record<string, string> = {
  confirmed: "Подтверждённые OEM-номера",
  low_confidence: "Под вопросом",
  irrelevant: "Отброшенные",
};

function Field({ k, v, serif }: { k: string; v: React.ReactNode; serif?: boolean }) {
  return (
    <div className="field">
      <div className="k">{k}</div>
      <div className={`v ${serif ? "serif" : ""}`}>{v ?? <span className="muted">—</span>}</div>
    </div>
  );
}

function Evidence({ rows }: { rows: ArticleRow[] }) {
  return (
    <div className="ev-list">
      {rows.map((a, i) => (
        <div className="ev" key={i}>
          <div className="ev-top"><span className="ev-art">{a.article}</span></div>
          <div className="ev-text">{a.evidence}</div>
          {a.why_low_confidence && <div className="ev-why">⚠ {a.why_low_confidence}</div>}
          {a.why_irrelevant && <div className="ev-why">⚠ {a.why_irrelevant}</div>}
          {a.source_url && (
            <a className="ev-link" href={a.source_url} target="_blank" rel="noreferrer">{a.source_url}</a>
          )}
        </div>
      ))}
    </div>
  );
}

export function RunDetail({ runId, onClose, onRetry }: {
  runId: number;
  onClose: () => void;
  onRetry: (article: string) => void;
}) {
  const [d, setD] = useState<Detail | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    setD(null);
    setErr(null);
    getRun(runId).then((x) => live && setD(x)).catch((e) => live && setErr(String(e)));
    return () => { live = false; };
  }, [runId]);

  useEffect(() => {
    const h = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [onClose]);

  const grouped = (c: ArticleRow["confidence"]) => d?.articles.filter((a) => a.confidence === c) ?? [];

  return (
    <>
      <div className="overlay" onClick={onClose} />
      <div className="sheet" role="dialog" aria-modal>
        <div className="sheet-head">
          <div>
            <div className="t">{d?.article ?? `run ${runId}`}</div>
            {d && <div className="muted" style={{ marginTop: 2 }}>задача {d.task_id} · run {d.run_id}</div>}
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            {d && <StatusBadge status={d.status} />}
            {d && isTerminal(d.status) && (
              <button
                className="retry-btn"
                title="Повторить ресерч — создаст новый run"
                onClick={() => { onRetry(d.article); onClose(); }}
              >
                <IconRefresh width={13} height={13} /> Повторить
              </button>
            )}
            <button className="icon-btn" onClick={onClose} aria-label="Закрыть"><IconClose /></button>
          </div>
        </div>

        <div className="sheet-body">
          {err && <div className="banner error">{err}</div>}
          {!d && !err && <div className="muted">Загрузка…</div>}

          {d && (
            <>
              {d.draft?.needs_review_reason && (
                <div className="banner review">Нужен человек: {d.draft.needs_review_reason}</div>
              )}
              {d.status.startsWith("failed") && d.error && (
                <div className="banner error">{d.error}</div>
              )}
              {d.publications.length > 0 && (
                <div className="banner published">
                  Опубликовано в Smart: {d.publications.map((p) => p.smart_id).join(", ")}
                </div>
              )}

              {d.draft && (
                <div>
                  <div className="section-label">Draft</div>
                  <div className="field-grid">
                    <Field k="Название" v={d.draft.name} serif />
                    <Field k="Тип" v={d.draft.product_type} />
                    <Field k="Бренды" v={d.draft.brand_oem.join(", ") || null} />
                    <Field k="Набор" v={d.draft.is_kit ? "да" : "нет"} />
                    <Field k="Вес, кг" v={d.draft.weight_kg ?? null} />
                    <Field k="Применяемость" v={d.draft.models_text} />
                  </div>
                  {d.draft.description && (
                    <div style={{ marginTop: 12 }}>
                      <Field k="Описание" v={d.draft.description} />
                    </div>
                  )}
                  {d.draft.weight_evidence && (
                    <div style={{ marginTop: 12 }}>
                      <Field k="Источник веса" v={d.draft.weight_evidence} />
                    </div>
                  )}
                </div>
              )}

              {(["confirmed", "low_confidence", "irrelevant"] as const).map((c) =>
                grouped(c).length ? (
                  <div key={c}>
                    <div className="section-label">{CONF_LABEL[c]} · {grouped(c).length}</div>
                    <Evidence rows={grouped(c)} />
                  </div>
                ) : null
              )}

              {d.components.length > 0 && (
                <div>
                  <div className="section-label">Состав набора · {d.components.length}</div>
                  <div className="ev-list">
                    {d.components.map((c, i) => (
                      <div className="ev" key={i}>
                        <div className="ev-top">
                          <span className="ev-art">{c.article ?? c.component_key}</span>
                          {c.quantity != null && <span className="muted">× {c.quantity}</span>}
                        </div>
                        <div className="ev-text">{c.name}{c.description ? ` — ${c.description}` : ""}</div>
                        <div className="ev-text" style={{ opacity: 0.8 }}>{c.evidence}</div>
                        {c.source_url && (
                          <a className="ev-link" href={c.source_url} target="_blank" rel="noreferrer">{c.source_url}</a>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {d.part_of_kits.length > 0 && (
                <div>
                  <div className="section-label">Входит в наборы · {d.part_of_kits.length}</div>
                  <div className="ev-list">
                    {d.part_of_kits.map((p, i) => (
                      <div className="ev" key={i}>
                        <div className="ev-top"><span className="ev-art">{p.kit_article ?? "— номер неизвестен —"}</span></div>
                        <div className="ev-text">{p.kit_name}</div>
                        <div className="ev-text" style={{ opacity: 0.8 }}>{p.evidence}</div>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {d.result_json != null && (
                <details className="disclosure">
                  <summary>Финальный JSON ({STATUS_LABEL[d.status] ?? d.status})</summary>
                  <pre className="json" style={{ marginTop: 10 }}>{JSON.stringify(d.result_json, null, 2)}</pre>
                </details>
              )}
            </>
          )}
        </div>
      </div>
    </>
  );
}
