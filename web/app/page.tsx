"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { getQueue, submitArticles, STATUS_LABEL, STATUS_ORDER, type QueueData, type TaskCard as TaskCardData } from "@/lib/api";
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
  const retry = useCallback(async (article: string) => {
    try { await submitArticles([article]); setErr(null); }
    catch (e) { setErr(String(e)); }
    refresh();
  }, [refresh]);

  useEffect(() => {
    refresh();
    timer.current = setInterval(refresh, 2000);
    return () => { if (timer.current) clearInterval(timer.current); };
  }, [refresh]);

  const counts = data?.counts ?? {};
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  const cards = data?.cards ?? [];
  const filtered = cards.filter((c) => matchesQuery(c, query));
  const filtering = query.trim().length > 0;

  return (
    <div className="app">
      <Rail footer={<WorkerStatus alive={!!data?.worker_alive} />}>
        <div className="rail-label">Добавить артикулы</div>
        <AddTasks onSubmitted={refresh} />
        <div className="rail-sep" />
        <div className="rail-label">Статусы</div>
        <div className="counts">
          {STATUS_ORDER.filter((s) => counts[s]).map((s) => (
            <div className="count-row" key={s}>
              <span className={`dot bdot ${FAILED.has(s) ? "failed_no_data" : s}`} />
              {STATUS_LABEL[s]}
              <span className="n">{counts[s]}</span>
            </div>
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
                <div>По запросу «{query.trim()}» задач нет. Измени запрос или очисти поиск.</div>
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
