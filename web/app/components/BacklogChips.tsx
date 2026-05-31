import type { QueueData } from "@/lib/api";

// Два бэклога пайплайна в виде чипов:
//   «В очереди»     = queued + running  — ещё не исследовано воркером;
//   «Ждёт куратора» = pending_publication — done без публикации в Smart.
export function BacklogChips({ data }: { data: QueueData | null }) {
  if (!data) return null;
  const c = data.counts ?? {};
  const inQueue = (c.queued ?? 0) + (c.running ?? 0);
  const pending = data.pending_publication ?? 0;
  return (
    <div className="backlog-chips">
      <div className="backlog-chip" title="queued + running — ещё не исследовано воркером">
        <span className="dot bdot queued" /> В очереди<span className="n">{inQueue}</span>
      </div>
      <div className="backlog-chip" title="done без публикации в Smart — бэклог куратора">
        <span className="dot bdot pending" /> Ждёт куратора<span className="n">{pending}</span>
      </div>
    </div>
  );
}
