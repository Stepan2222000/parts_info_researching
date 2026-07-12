"use client";

import { useCallback, useEffect, useState } from "react";
import {
  getRun, getRunTurns, isTerminal, ALL_STAGES, PRESET_LABEL, STAGE_LABEL, STATUS_LABEL,
  type ArticleRow, type Profile, type RunDetail as Detail, type StageOutcomes,
  type StructuredSnapshot, type TurnRow, type TurnsPayload,
} from "@/lib/api";
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

function Evidence({ rows, current }: { rows: ArticleRow[]; current?: string | null }) {
  return (
    <div className="ev-list">
      {rows.map((a, i) => (
        <div className="ev" key={i}>
          <div className="ev-top">
            <span className="ev-art">{a.article}</span>
            {current && a.article === current && <span className="badge-current">текущий</span>}
          </div>
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

type SupEdge = Detail["supersession"][number];

// «Текущий» = номер, кого никто не заменял (вершина), если он единственный.
function currentNumber(sup: SupEdge[]): string | null {
  if (!sup.length) return null;
  const olders = new Set(sup.map((s) => s.older));
  const all = new Set(sup.flatMap((s) => [s.newer, s.older]));
  const tops = [...all].filter((n) => !olders.has(n));
  return tops.length === 1 ? tops[0] : null;
}

// Складываем пары в одну линию старый→новый; не получилось чисто (развилка/цикл) → null.
function buildChain(sup: SupEdge[]): string[] | null {
  if (!sup.length) return null;
  const newerOf = new Map<string, string>();
  const olderOf = new Map<string, string>();
  const nodes = new Set<string>();
  for (const s of sup) {
    nodes.add(s.older);
    nodes.add(s.newer);
    if (newerOf.has(s.older) || olderOf.has(s.newer)) return null; // развилка
    newerOf.set(s.older, s.newer);
    olderOf.set(s.newer, s.older);
  }
  const oldest = [...nodes].filter((n) => !olderOf.has(n));
  if (oldest.length !== 1) return null;
  const chain: string[] = [];
  const seen = new Set<string>();
  let cur: string | undefined = oldest[0];
  while (cur !== undefined) {
    if (seen.has(cur)) return null; // цикл
    seen.add(cur);
    chain.push(cur);
    cur = newerOf.get(cur);
  }
  return chain.length === nodes.size ? chain : null;
}

// Развилка (порядок не одна линия) → группируем по новому: «<новый> заменяет <старые>».
function groupByNewer(sup: SupEdge[]): { newer: string; olders: string[] }[] {
  const m = new Map<string, string[]>();
  for (const s of sup) {
    const arr = m.get(s.newer) ?? [];
    if (!arr.includes(s.older)) arr.push(s.older);
    m.set(s.newer, arr);
  }
  return [...m.entries()].map(([newer, olders]) => ({ newer, olders }));
}

function Nuances({ d }: { d: Detail }) {
  const sup = d.supersession ?? [];
  const nu = d.nuances ?? [];
  if (!sup.length && !nu.length) return null;
  const chain = buildChain(sup);
  return (
    <div className="nuance">
      <div className="section-label">Отличия и нюансы</div>
      {sup.length > 0 &&
        (chain ? (
          <div className="nuance-chain">
            <span className="lead">порядок (старый → новый):</span>
            {chain.map((n, i) => (
              <span key={n} className="chain-seg">
                {i > 0 && <span className="chain-arrow">→</span>}
                <span className={`chain-chip${i === chain.length - 1 ? " current" : ""}`}>{n}</span>
              </span>
            ))}
          </div>
        ) : (
          <div className="nuance-groups">
            {groupByNewer(sup).map((g) => (
              <div key={g.newer} className="sup-group">
                <span className="chain-chip current">{g.newer}</span>
                <span className="chain-arrow">заменяет</span>
                {g.olders.map((o) => (
                  <span key={o} className="chain-chip">{o}</span>
                ))}
              </div>
            ))}
          </div>
        ))}
      {nu.length > 0 && (
        <div className="nuance-cav">
          {nu.map((n, i) => (
            <div key={i}>
              <div className="cav-text">
                • {n.text}
                {n.articles.length > 0 && <span className="cav-for"> — касается: {n.articles.join(", ")}</span>}
              </div>
              {n.evidence && <div className="cav-ev">{n.evidence}</div>}
              {n.source_url && (
                <a className="ev-link" href={n.source_url} target="_blank" rel="noreferrer">пруф ↗</a>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ── живой ран: снапшот последнего турна -> та же форма, что и draft-detail ──────
// Пока ран идёт, draft-таблиц ещё нет — рендерим ровно те же секции из
// result_json-снапшота, чтобы финальный и живой вид не расходились.
function liveDetail(d: Detail, snap: StructuredSnapshot): Detail {
  const numbers = snap.numbers ?? {};
  const articles: ArticleRow[] = [
    ...(numbers.article ?? []).map((a) => ({
      article: a.article, confidence: "confirmed" as const,
      source_url: a.source_url, evidence: a.evidence,
      why_low_confidence: null, why_irrelevant: null,
    })),
    ...(numbers.article_low_confidence ?? []).map((a) => ({
      article: a.article, confidence: "low_confidence" as const,
      source_url: a.source_url, evidence: a.evidence,
      why_low_confidence: a.why_low_confidence, why_irrelevant: null,
    })),
    ...(numbers.irrelevant ?? []).map((a) => ({
      article: a.article, confidence: "irrelevant" as const,
      source_url: a.source_url, evidence: a.evidence,
      why_low_confidence: null, why_irrelevant: a.why_irrelevant,
    })),
  ];
  let unknown = 0;
  return {
    ...d,
    draft: {
      name: snap.name ?? null,
      name_en: snap.name_en ?? null,
      brand_oem: snap.brand_oem ?? [],
      product_type: null,
      vehicle_classes: snap.vehicle_classes ?? [],
      description: snap.description ?? null,
      description_en: snap.description_en ?? null,
      is_kit: snap.is_kit ?? false,
      weight_kg: snap.weight?.kg ?? null,
      weight_source_url: snap.weight?.source_url ?? null,
      weight_evidence: snap.weight?.evidence ?? null,
      models_text: snap.models?.text ?? null,
      models_source_urls: snap.models?.source_urls ?? [],
      models_evidence: snap.models?.evidence ?? null,
      needs_review_reason: null,
    },
    articles,
    nuances: snap.nuances ?? [],
    supersession: snap.supersession ?? [],
    components: (snap.kit_contents ?? []).map((c) => ({
      component_key: c.article ?? `unknown_${++unknown}`,
      article: c.article,
      name: c.name,
      quantity: c.quantity,
      description: c.description,
      source_url: c.source_url,
      evidence: c.evidence,
    })),
    part_of_kits: snap.part_of_kits ?? [],
    prices: (snap.us_prices ?? []).map((p) => ({
      site: p.site, price: p.price, currency: p.currency ?? "USD",
      url: p.url, in_stock: p.in_stock, article: p.article, evidence: p.evidence,
    })),
  };
}

// ── таймлайн этапов ──────────────────────────────────────────────────────────────
type TimelineRow = {
  stage: string;
  state: "ok" | "failed" | "running" | "repairing" | "pending" | "not_applicable" | "skipped_by_profile";
  repaired: boolean; // ok после repair-попытки ("ok (repaired)")
  error: string | null;
  duration_s: number | null;
  summary: string | null;
};

function timelineRows(
  profile: Profile | null,
  outcomes: StageOutcomes | null,
  turns: TurnRow[],
): TimelineRow[] {
  const enabled = new Set(profile?.stages ?? []);
  const lastTurnByStage = new Map<string, TurnRow>();
  for (const t of turns) lastTurnByStage.set(t.stage, t);
  return ALL_STAGES.map((stage) => {
    // Исход: из stage_outcomes; до старта рана (queued) — планируем по профилю.
    const raw = outcomes?.[stage]
      ?? (stage === "main" || stage === "kit_contents" || !profile || enabled.has(stage)
        ? "pending" : "skipped_by_profile");
    const t = lastTurnByStage.get(stage);
    let state: TimelineRow["state"];
    let repaired = false;
    let error: string | null = null;
    if (raw === "ok" || raw === "running" || raw === "pending"
      || raw === "not_applicable" || raw === "skipped_by_profile") {
      state = raw;
    } else if (raw === "ok (repaired)") {
      state = "ok";
      repaired = true;
    } else if (raw.startsWith("repairing")) {
      state = "repairing";
      error = raw.replace(/^repairing:\s*/, "");
    } else if (raw.startsWith("failed")) {
      state = "failed";
      error = raw.replace(/^failed:\s*/, "");
    } else {
      state = "pending";
    }
    return {
      stage,
      state,
      repaired,
      error,
      duration_s: t?.duration_s ?? null,
      summary: t?.status === "ok" ? t.summary : null,
    };
  });
}

const TL_STATE_LABEL: Record<TimelineRow["state"], string> = {
  ok: "",
  failed: "этап упал",
  running: "идёт сейчас",
  repairing: "ошибка валидации — агент исправляет",
  pending: "впереди",
  not_applicable: "не потребовался",
  skipped_by_profile: "выключен профилем",
};

function fmtDuration(s: number | null): string | null {
  if (s == null) return null;
  if (s < 90) return `${Math.round(s)}с`;
  return `${Math.floor(s / 60)}м ${Math.round(s % 60)}с`;
}

function StageTimeline({ rows, queuePosition }: { rows: TimelineRow[]; queuePosition: number | null }) {
  return (
    <div>
      <div className="section-label">Этапы</div>
      {queuePosition != null && (
        <div className="muted" style={{ marginBottom: 10 }}>
          В очереди{queuePosition > 0 ? ` · позиция ${queuePosition + 1}` : " · следующий"}
        </div>
      )}
      <div className="tl">
        {rows.map((r) => (
          <div className={`tl-row ${r.state}`} key={r.stage}>
            <span className={`tl-dot ${r.state}`}>
              {r.state === "ok" && "✓"}
              {r.state === "failed" && "✕"}
            </span>
            <div className="tl-body">
              <div className="tl-head">
                <span className="tl-stage">{STAGE_LABEL[r.stage] ?? r.stage}</span>
                <span className="tl-note">
                  {r.state === "ok"
                    ? [r.repaired ? "исправлено агентом" : null, fmtDuration(r.duration_s)]
                        .filter(Boolean).join(" · ")
                    : TL_STATE_LABEL[r.state]}
                </span>
              </div>
              {r.summary && r.summary !== "без содержательных изменений" && (
                <div className="tl-summary">{r.summary}</div>
              )}
              {r.error && <div className="tl-error" title={r.error}>{r.error}</div>}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

// ── панель рана ──────────────────────────────────────────────────────────────────
export function RunDetail({ runId, onClose, onRetry }: {
  runId: number;
  onClose: () => void;
  onRetry: (article: string, profile: Profile | null) => void;
}) {
  const [d, setD] = useState<Detail | null>(null);
  const [turns, setTurns] = useState<TurnsPayload | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    const [det, tp] = await Promise.all([getRun(runId), getRunTurns(runId)]);
    setD(det);
    setTurns(tp);
    setErr(null);
  }, [runId]);

  useEffect(() => {
    let live = true;
    setD(null);
    setTurns(null);
    setErr(null);
    refresh().catch((e) => live && setErr(String(e)));
    return () => { live = false; };
  }, [refresh]);

  // Живой поллинг, пока ран не терминален (та же частота, что у дашборда).
  const active = d != null && !isTerminal(d.status);
  useEffect(() => {
    if (!active) return;
    const t = setInterval(() => { refresh().catch((e) => setErr(String(e))); }, 2000);
    return () => clearInterval(t);
  }, [active, refresh]);

  useEffect(() => {
    const h = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [onClose]);

  // Живой ран: рендерим секции из снапшота последнего турна; финальный — из draft.
  const view = d && !d.draft && turns?.snapshot ? liveDetail(d, turns.snapshot) : d;
  const tlRows = d && turns && !turns.legacy_run
    ? timelineRows(d.profile, d.stage_outcomes, turns.turns)
    : null;
  // «Что нового»: сводка последнего содержательного ok-турна (баннер живого рана).
  const lastSummary = active
    ? [...(turns?.turns ?? [])].reverse().find(
        (t) => t.status === "ok" && t.summary && t.summary !== "без содержательных изменений")?.summary ?? null
    : null;

  const grouped = (c: ArticleRow["confidence"]) => view?.articles.filter((a) => a.confidence === c) ?? [];
  const preset = d?.profile?.preset;

  return (
    <>
      <div className="overlay" onClick={onClose} />
      <div className="sheet" role="dialog" aria-modal>
        <div className="sheet-head">
          <div>
            <div className="t">{d?.article ?? `run ${runId}`}</div>
            {d && (
              <div className="muted" style={{ marginTop: 2 }}>
                задача {d.task_id} · run {d.run_id}
                {preset && <> · профиль: {PRESET_LABEL[preset] ?? preset}</>}
              </div>
            )}
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            {d && <StatusBadge status={d.status} />}
            {d && isTerminal(d.status) && (
              <button
                className="retry-btn"
                title="Повторить ресерч (тот же профиль) — создаст новый run"
                onClick={() => { onRetry(d.article, d.profile); onClose(); }}
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

          {d && view && (
            <>
              {lastSummary && <div className="banner delta">Новое: {lastSummary}</div>}
              {view.draft?.needs_review_reason && (
                <div className="banner review">Нужен человек: {view.draft.needs_review_reason}</div>
              )}
              {d.status.startsWith("failed") && d.error && (
                <div className="banner error">{d.error}</div>
              )}
              {d.publications.length > 0 && (
                <div className="banner published">
                  Опубликовано в Smart: {d.publications.map((p) => p.smart_id).join(", ")}
                </div>
              )}

              {tlRows && <StageTimeline rows={tlRows} queuePosition={turns?.progress.queue_position ?? null} />}

              {view.draft && (
                <div>
                  <div className="section-label">{d.draft ? "Draft" : "Собрано на текущий момент"}</div>
                  <div className="field-grid">
                    <Field k="Название" v={view.draft.name} serif />
                    <Field k="Name (EN)" v={view.draft.name_en} serif />
                    <Field k="Классы" v={view.draft.vehicle_classes.join(", ") || null} />
                    <Field k="Тип" v={view.draft.product_type} />
                    <Field k="Бренды" v={view.draft.brand_oem.join(", ") || null} />
                    <Field k="Набор" v={view.draft.is_kit ? "да" : "нет"} />
                    <Field k="Вес, кг" v={view.draft.weight_kg ?? null} />
                    <Field k="Применяемость" v={view.draft.models_text} />
                  </div>
                  {view.draft.description && (
                    <div style={{ marginTop: 12 }}>
                      <Field k="Описание" v={view.draft.description} />
                    </div>
                  )}
                  {view.draft.description_en && (
                    <div style={{ marginTop: 12 }}>
                      <Field k="Description (EN)" v={view.draft.description_en} />
                    </div>
                  )}
                  {view.draft.weight_evidence && (
                    <div style={{ marginTop: 12 }}>
                      <Field k="Источник веса" v={view.draft.weight_evidence} />
                    </div>
                  )}
                </div>
              )}

              <Nuances d={view} />

              {(["confirmed", "low_confidence", "irrelevant"] as const).map((c) =>
                grouped(c).length ? (
                  <div key={c}>
                    <div className="section-label">{CONF_LABEL[c]} · {grouped(c).length}</div>
                    <Evidence rows={grouped(c)} current={c === "confirmed" ? currentNumber(view.supersession ?? []) : null} />
                  </div>
                ) : null
              )}

              {view.components.length > 0 && (
                <div>
                  <div className="section-label">Состав набора · {view.components.length}</div>
                  <div className="ev-list">
                    {view.components.map((c, i) => (
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

              {view.part_of_kits.length > 0 && (
                <div>
                  <div className="section-label">Входит в наборы · {view.part_of_kits.length}</div>
                  <div className="ev-list">
                    {view.part_of_kits.map((p, i) => (
                      <div className="ev" key={i}>
                        <div className="ev-top"><span className="ev-art">{p.kit_article ?? "— номер неизвестен —"}</span></div>
                        <div className="ev-text">{p.kit_name}</div>
                        <div className="ev-text" style={{ opacity: 0.8 }}>{p.evidence}</div>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {view.prices.length > 0 && (
                <div>
                  <div className="section-label">Цены US (за оригинал) · {view.prices.length}</div>
                  <div className="ev-list">
                    {view.prices.map((p, i) => (
                      <div className="ev" key={i}>
                        <div className="ev-top">
                          <span className="ev-art">{p.price.toFixed(2)} {p.currency}</span>
                          <span className="muted">{p.site}{p.in_stock === false ? " · нет в наличии" : ""}</span>
                        </div>
                        {p.article && <div className="ev-text" style={{ opacity: 0.8 }}>номер: {p.article}</div>}
                        {p.url && (
                          <a className="ev-link" href={p.url} target="_blank" rel="noreferrer">{p.url}</a>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {d.result_json != null && (
                <details className="disclosure">
                  <summary>
                    {isTerminal(d.status)
                      ? `Финальный JSON (${STATUS_LABEL[d.status] ?? d.status})`
                      : "Промежуточный JSON (растёт по турнам)"}
                  </summary>
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
