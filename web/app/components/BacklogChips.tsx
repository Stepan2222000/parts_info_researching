import type { QueueData } from "@/lib/api";

// Бэклоги пайплайна в виде чипов:
//   «В очереди»     = queued + running  — ещё не исследовано воркером;
//   «Ждёт куратора» = pending_publication — done без публикации в Smart;
//   «Hard list»     = автопроход отказался с причиной — кликабельный фильтр;
//   «На разбор»     = needs_human_review — припарковано, разбирается по команде «разобрать».
export function BacklogChips({
  data,
  hardListActive = false,
  onToggleHardList,
}: {
  data: QueueData | null;
  hardListActive?: boolean;
  // Без обработчика (страница чата) чип «Hard list» — просто счётчик, не фильтр.
  onToggleHardList?: () => void;
}) {
  if (!data) return null;
  const c = data.counts ?? {};
  const inQueue = (c.queued ?? 0) + (c.running ?? 0);
  const pending = data.pending_publication ?? 0;
  const hardList = data.hard_list ?? 0;
  const needsReview = c.needs_human_review ?? 0;
  return (
    <div className="backlog-chips">
      <div className="backlog-chip" title="queued + running — ещё не исследовано воркером">
        <span className="dot bdot queued" /> В очереди<span className="n">{inQueue}</span>
      </div>
      <div className="backlog-chip" title="done без публикации в Smart — бэклог куратора">
        <span className="dot bdot pending" /> Ждёт куратора<span className="n">{pending}</span>
      </div>
      {hardList > 0 && (onToggleHardList ? (
        <button
          type="button"
          className={`backlog-chip clickable${hardListActive ? " active" : ""}`}
          onClick={onToggleHardList}
          aria-pressed={hardListActive}
          title="Автопроход отказался публиковать (причина на карточке) — очередь LLM-куратора"
        >
          <span className="dot bdot failed_validation" /> Hard list<span className="n">{hardList}</span>
        </button>
      ) : (
        <div className="backlog-chip" title="Автопроход отказался публиковать — очередь LLM-куратора">
          <span className="dot bdot failed_validation" /> Hard list<span className="n">{hardList}</span>
        </div>
      ))}
      <div className="backlog-chip" title="needs_human_review — припарковано, разбирается по команде «разобрать»">
        <span className="dot bdot needs_human_review" /> На разбор<span className="n">{needsReview}</span>
      </div>
    </div>
  );
}
