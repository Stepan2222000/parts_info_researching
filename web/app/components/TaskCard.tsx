"use client";

import type { Profile, TaskCard as Card } from "@/lib/api";
import { isTerminal, PRESET_LABEL, STAGE_LABEL } from "@/lib/api";
import { StatusBadge } from "./StatusBadge";
import { IconBox, IconCheck, IconRefresh } from "@/lib/icons";

export function TaskCard({
  card,
  onOpen,
  onRetry,
}: {
  card: Card;
  onOpen: () => void;
  onRetry: (article: string, profile: Profile | null) => void;
}) {
  const preset = card.profile?.preset;
  return (
    <div className="card" onClick={onOpen} role="button" tabIndex={0}>
      <div className="card-top">
        <span className="card-art">{card.article}</span>
        <StatusBadge status={card.status} />
      </div>
      <div className="card-name">{card.name ?? <span className="muted">— без названия —</span>}</div>
      <div className="card-meta">
        {(card.brand_oem ?? []).map((b) => (
          <span className="chip" key={b}>{b}</span>
        ))}
        {(card.vehicle_classes ?? []).map((vc) => (
          <span className="chip" key={vc}>{vc}</span>
        ))}
      </div>
      <div className="card-foot">
        {card.status === "running" && card.current_stage && (
          <span className="stage-chip">
            <span className="stage-pulse" />
            {STAGE_LABEL[card.current_stage] ?? card.current_stage}
          </span>
        )}
        {card.is_kit && (
          <span className="badge kit"><IconBox width={12} height={12} /> Набор</span>
        )}
        {card.published && (
          <span className="badge published"><IconCheck width={12} height={12} /> В Smart</span>
        )}
        {preset && preset !== "default" && (
          <span className="chip" title={`Профиль этапов: ${(card.profile?.stages ?? []).join(", ") || "только ядро"}`}>
            {PRESET_LABEL[preset] ?? preset}
          </span>
        )}
        <span className="muted" style={{ marginLeft: "auto" }}>run {card.run_id}</span>
        {isTerminal(card.status) && (
          <button
            className="retry-btn"
            title="Повторить ресерч (тот же профиль этапов) — создаст новый run"
            onClick={(e) => { e.stopPropagation(); onRetry(card.article, card.profile); }}
          >
            <IconRefresh width={13} height={13} /> Повторить
          </button>
        )}
      </div>
    </div>
  );
}
