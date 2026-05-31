"use client";

import type { TaskCard as Card } from "@/lib/api";
import { isTerminal } from "@/lib/api";
import { StatusBadge } from "./StatusBadge";
import { IconBox, IconCheck, IconRefresh } from "@/lib/icons";

export function TaskCard({
  card,
  onOpen,
  onRetry,
}: {
  card: Card;
  onOpen: () => void;
  onRetry: (article: string) => void;
}) {
  return (
    <div className="card" onClick={onOpen} role="button" tabIndex={0}>
      <div className="card-top">
        <span className="card-art">{card.article}</span>
        <StatusBadge status={card.status} />
      </div>
      <div className="card-name">{card.name ?? <span className="muted">— без названия —</span>}</div>
      <div className="card-meta">
        {card.brand_oem.map((b) => (
          <span className="chip" key={b}>{b}</span>
        ))}
        {card.product_type && <span className="chip">{card.product_type}</span>}
      </div>
      <div className="card-foot">
        {card.is_kit && (
          <span className="badge kit"><IconBox width={12} height={12} /> Набор</span>
        )}
        {card.published && (
          <span className="badge published"><IconCheck width={12} height={12} /> В Smart</span>
        )}
        <span className="muted" style={{ marginLeft: "auto" }}>run {card.run_id}</span>
        {isTerminal(card.status) && (
          <button
            className="retry-btn"
            title="Повторить ресерч — создаст новый run"
            onClick={(e) => { e.stopPropagation(); onRetry(card.article); }}
          >
            <IconRefresh width={13} height={13} /> Повторить
          </button>
        )}
      </div>
    </div>
  );
}
