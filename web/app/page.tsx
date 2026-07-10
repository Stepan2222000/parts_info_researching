"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { getQueue, submitArticles, STATUS_LABEL, STATUS_ORDER, type Profile, type QueueData, type RunStatus, type TaskCard as TaskCardData } from "@/lib/api";
import { Rail, WorkerStatus } from "@/components/Rail";
import { AddTasks } from "@/components/AddTasks";
import { TaskCard } from "@/components/TaskCard";
import { RunDetail } from "@/components/RunDetail";
import { BacklogChips } from "@/components/BacklogChips";
import { IconRefresh } from "@/lib/icons";

const FAILED = new Set(["failed_no_data", "failed_validation", "failed_crashed"]);

// Поиск живёт ТОЛЬКО на клиенте: фронт держит весь список (бэкенд отдаёт всё без
// лимита), фильтруем в памяти по всему, что видно на карточке. AND по словам.
function matchesQuery(card: TaskCardData, query: string): boolean {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  const text = [
    card.article,
    card.name ?? "",
    (card.brand_oem ?? []).join(" "),
    card.product_type ?? "",
    (card.vehicle_classes ?? []).join(" "),
    STATUS_LABEL[card.status] ?? card.status,
    card.is_kit ? "набор" : "",
    card.published ? "в smart" : "",
  ].join(" ").toLowerCase();
  // Форма без разделителей — чтобы «807252-t5» / «807252 t5» находило «807252T5».
  const alnum = text.replace(/[^a-z0-9]/gi, "");
  return q.split(/\s+/).every((tok) => {
    if (!tok) return true;
    if (text.includes(tok)) return true;
    const tokAlnum = tok.replace(/[^a-z0-9]/gi, "");
    return tokAlnum.length > 0 && alnum.includes(tokAlnum);
  });
}

export default function Dashboard() {
  const [data, setData] = useState<QueueData | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [openRun, setOpenRun] = useState<number | null>(null);
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState<RunStatus | null>(null);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  const refresh = useCallback(async () => {
    try {
      setData(await getQueue());
      setErr(null);
    } catch (e) {
      setErr(String(e));
    }
  }, []);

  // Ретрай: повторный submit того же артикула → submit_article создаёт новый run.
  // Профиль наследуется от исходного рана («повторить то же исследование»).
  const retry = useCallback(async (article: string, profile: Profile | null) => {
    try { await submitArticles([article], profile ?? undefined); setErr(null); }
    catch (e) { setErr(String(e)); }
    refresh();
  }, [refresh]);

  useEffect(() => {
    refresh();
    timer.current = setInterval(refresh, 2000);
    return () => { if (timer.current) clearInterval(timer.current); };
  }, [refresh]);

  // Deep-link на ран: ?run=123 открывает панель (можно шарить ссылку/возвращаться
  // с другого компа). URL обновляем без перезагрузки и без записи в history.
  useEffect(() => {
    const fromUrl = new URLSearchParams(window.location.search).get("run");
    if (fromUrl && /^\d+$/.test(fromUrl)) setOpenRun(Number(fromUrl));
  }, []);
  useEffect(() => {
    const url = new URL(window.location.href);
    if (openRun != null) url.searchParams.set("run", String(openRun));
    else url.searchParams.delete("run");
    window.history.replaceState(null, "", url);
  }, [openRun]);

  const counts = data?.counts ?? {};
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  const cards = data?.cards ?? [];
  const filtered = cards.filter(
    (c) => matchesQuery(c, query) && (statusFilter === null || c.status === statusFilter),
  );
  const filtering = query.trim().length > 0 || statusFilter !== null;

  return (
    <div className="app">
      <Rail footer={<WorkerStatus alive={!!data?.worker_alive} />}>
        <div className="rail-label">Добавить артикулы</div>
        <AddTasks onSubmitted={refresh} />
        <div className="rail-sep" />
        <div className="rail-label">Статусы</div>
        <div className="counts">
          {STATUS_ORDER.filter((s) => counts[s]).map((s) => (
            <button
              type="button"
              className={`count-row${statusFilter === s ? " active" : ""}`}
              key={s}
              onClick={() => setStatusFilter((cur) => (cur === s ? null : s))}
              aria-pressed={statusFilter === s}
              title={statusFilter === s ? "Снять фильтр" : `Показать только: ${STATUS_LABEL[s]}`}
            >
              <span className={`dot bdot ${FAILED.has(s) ? "failed_no_data" : s}`} />
              {STATUS_LABEL[s]}
              <span className="n">{counts[s]}</span>
            </button>
          ))}
          {total === 0 && <div className="muted" style={{ padding: "6px 9px" }}>Очередь пуста.</div>}
        </div>
      </Rail>

      <div className="main">
        <div className="topbar">
          <div>
            <h1>Очередь</h1>
            <span className="sub">
              {filtering
                ? `Найдено ${filtered.length} из ${cards.length}`
                : `${total} задач · обновляется автоматически`}
            </span>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
            <div className="search">
              <input
                className="search-input"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Поиск по артикулу, названию, бренду…"
                aria-label="Поиск по очереди"
              />
              {filtering && (
                <button className="search-clear" onClick={() => setQuery("")} aria-label="Очистить поиск">×</button>
              )}
            </div>
            <BacklogChips data={data} />
            <button className="icon-btn" onClick={refresh} aria-label="Обновить"><IconRefresh /></button>
          </div>
        </div>

        <div className="scroll">
          <div className="wrap">
            {err && <div className="banner error" style={{ marginBottom: 18 }}>{err}</div>}
            {data && cards.length === 0 && (
              <div className="empty">
                <div className="serif">Пока пусто.</div>
                <div>Добавь артикулы слева, чтобы поставить их в очередь ресерча.</div>
              </div>
            )}
            {data && cards.length > 0 && filtered.length === 0 && (
              <div className="empty">
                <div className="serif">Ничего не найдено.</div>
                <div>
                  {query.trim()
                    ? `По запросу «${query.trim()}»${statusFilter ? ` и статусу «${STATUS_LABEL[statusFilter]}»` : ""} задач нет. Измени запрос или сними фильтр.`
                    : `Нет задач со статусом «${statusFilter ? STATUS_LABEL[statusFilter] : ""}».`}
                </div>
              </div>
            )}
            <div className="cards">
              {filtered.map((c) => (
                <TaskCard key={c.task_id} card={c} onOpen={() => setOpenRun(c.run_id)} onRetry={retry} />
              ))}
            </div>
          </div>
        </div>
      </div>

      {openRun != null && <RunDetail runId={openRun} onClose={() => setOpenRun(null)} onRetry={retry} />}
    </div>
  );
}
