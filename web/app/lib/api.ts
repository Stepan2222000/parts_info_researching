// Types + client fetchers. The browser talks only to same-origin Next route
// handlers under /api/*, which proxy to the Python FastAPI process server-side.

export type RunStatus =
  | "queued" | "running" | "done"
  | "failed_no_data" | "failed_validation" | "failed_crashed"
  | "needs_human_review" | "skipped_smart_approved";

export const STATUS_ORDER: RunStatus[] = [
  "queued", "running", "done", "needs_human_review", "skipped_smart_approved",
  "failed_no_data", "failed_validation", "failed_crashed",
];

export const STATUS_LABEL: Record<string, string> = {
  queued: "В очереди",
  running: "В работе",
  done: "Готово",
  needs_human_review: "Нужен человек",
  skipped_smart_approved: "Smart утверждён",
  failed_no_data: "Нет данных",
  failed_validation: "Ошибка валидации",
  failed_crashed: "Сбой",
};

// Терминальные статусы — на них доступен ретрай (повторный submit → новый run).
export const TERMINAL_STATUSES: RunStatus[] = [
  "done", "needs_human_review", "skipped_smart_approved",
  "failed_no_data", "failed_validation", "failed_crashed",
];
export const isTerminal = (s: string): boolean => (TERMINAL_STATUSES as string[]).includes(s);

// ── профили этапов и прогресс ────────────────────────────────────────────────
export interface Profile {
  preset: string;          // fast | default | full | custom
  stages: string[];        // включённые ОПЦИОНАЛЬНЫЕ этапы
}

export const PRESET_LABEL: Record<string, string> = {
  fast: "Быстрый",
  default: "Стандарт",
  full: "Полный",
  custom: "Кастомный",
};

// Порядок = порядок исполнения пайплайна.
export const ALL_STAGES = [
  "main", "family_expansion", "low_confidence", "kit_contents",
  "price_fallback", "difference", "phase2",
] as const;

export const STAGE_LABEL: Record<string, string> = {
  main: "Основной поиск",
  family_expansion: "Семейство кроссов",
  low_confidence: "Сомнительные номера",
  kit_contents: "Состав набора",
  price_fallback: "Цены US",
  difference: "Нюансы и замены",
  phase2: "Свободный добор",
};

// Исход этапа: ok | pending | running | not_applicable | skipped_by_profile | "failed: <текст>"
export type StageOutcomes = Record<string, string>;

export interface TurnRow {
  turn_idx: number;
  stage: string;
  status: "running" | "ok" | "failed";
  started_at: string | null;
  finished_at: string | null;
  duration_s: number | null;
  error: string | null;
  summary: string | null;   // русская сводка «что нового» (у ok-турнов)
}

export interface TurnsPayload {
  run_id: number;
  task_id: number;
  article: string;
  status: RunStatus;
  is_final: boolean;
  error: string | null;
  profile: Profile | null;
  stage_outcomes: StageOutcomes | null;
  progress: { current_stage: string | null; turns_done: number; queue_position: number | null };
  latest_turn: number;
  legacy_run: boolean;
  turns: TurnRow[];
  delta: { changed: Record<string, unknown>; summary: string } | null;
  snapshot: StructuredSnapshot | null;
}

// Форма result_json/снапшота (StructuredResult) — только то, что рендерит UI.
export interface StructuredSnapshot {
  task_part_number?: string;
  name?: string | null;
  name_en?: string | null;
  brand_oem?: string[];
  vehicle_classes?: string[];
  description?: string | null;
  description_en?: string | null;
  weight?: { kg: number; source_url: string; evidence: string } | null;
  models?: { text: string; source_urls: string[]; evidence: string } | null;
  is_kit?: boolean;
  kit_contents?: {
    article: string | null; name: string; name_en: string | null; quantity: number | null;
    description: string | null; description_en: string | null; source_url: string; evidence: string;
  }[];
  part_of_kits?: { kit_article: string | null; kit_name: string | null; source_url: string; evidence: string }[];
  numbers?: {
    article?: { article: string; source_url: string; evidence: string }[];
    article_low_confidence?: { article: string; source_url: string; evidence: string; why_low_confidence: string }[];
    irrelevant?: { article: string; source_url: string; evidence: string; why_irrelevant: string }[];
  };
  us_prices?: {
    site: string; price: number; currency: string | null; url: string;
    article: string; in_stock: boolean | null; evidence: string;
  }[];
  nuances?: Nuance[];
  supersession?: SupersessionEdge[];
}

export interface TaskCard {
  task_id: number;
  article: string;
  run_id: number;
  status: RunStatus;
  error: string | null;
  profile: Profile | null;
  current_stage: string | null;   // running-этап (для чипа прогресса)
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  name: string | null;
  product_type: string | null;
  vehicle_classes: string[];
  is_kit: boolean | null;
  brand_oem: string[];
  published: boolean;
}

export interface QueueData {
  counts: Record<string, number>;
  cards: TaskCard[];
  worker_alive: boolean;
  // Бэклог куратора: done-раны без публикации в Smart («ещё не обработано»).
  pending_publication: number;
}

export interface SubmitResult {
  article: string;
  status: "queued" | "invalid" | "refused";
  error: string | null;
  task_id: number | null;
  run_id: number | null;
  reused: boolean;
}

export interface ArticleRow {
  article: string;
  confidence: "confirmed" | "low_confidence" | "irrelevant";
  source_url: string;
  evidence: string;
  why_low_confidence: string | null;
  why_irrelevant: string | null;
}

export interface Nuance {
  text: string;
  articles: string[];   // [] = ко всей детали; иначе — конкретные номера
  source_url: string;
  evidence: string;
}

export interface SupersessionEdge {
  newer: string;
  older: string;
  source_url: string;
  evidence: string;
}

export interface ComponentRow {
  component_key: string;
  article: string | null;
  name: string | null;
  quantity: number | null;
  description: string | null;
  source_url: string;
  evidence: string;
}

export interface RunDetail {
  run_id: number;
  task_id: number;
  article: string;
  status: RunStatus;
  error: string | null;
  profile: Profile | null;
  stage_outcomes: StageOutcomes | null;
  result_json: StructuredSnapshot | null;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  draft: {
    name: string | null;
    name_en: string | null;
    brand_oem: string[];
    product_type: string | null;
    vehicle_classes: string[];
    description: string | null;
    description_en: string | null;
    is_kit: boolean;
    weight_kg: number | null;
    weight_source_url: string | null;
    weight_evidence: string | null;
    models_text: string | null;
    models_source_urls: string[];
    models_evidence: string | null;
    needs_review_reason: string | null;
  } | null;
  articles: ArticleRow[];
  nuances: Nuance[];
  supersession: SupersessionEdge[];
  components: ComponentRow[];
  part_of_kits: { kit_article: string | null; kit_name: string | null; source_url: string; evidence: string }[];
  publications: { id: number; smart_id: string; curator_session_id: number; published_at: string | null }[];
  prices: PriceRow[];
}

export interface PriceRow {
  site: string;
  price: number;
  currency: string;
  url: string | null;
  in_stock: boolean | null;
  article: string | null;
  evidence: string | null;
}

export interface SessionSummary {
  session_id: number;
  started_at: string | null;
  ended_at: string | null;
  message_count: number;
}

async function jget<T>(url: string): Promise<T> {
  const r = await fetch(url, { cache: "no-store" });
  if (!r.ok) throw new Error(`${url} → ${r.status} ${await r.text()}`);
  return r.json();
}

export const getQueue = () => jget<QueueData>("/api/queue");
export const getRun = (id: number) => jget<RunDetail>(`/api/runs/${id}`);
// Живой прогресс: снапшот всегда полный (UI не ведёт курсор — простая замена состояния).
export const getRunTurns = (id: number) =>
  jget<TurnsPayload>(`/api/runs/${id}/turns?since=0&mode=snapshot`);
export const getSessions = () => jget<{ sessions: SessionSummary[] }>("/api/curator/sessions");

export async function submitArticles(
  articles: string[],
  profile?: Profile | string | null,
): Promise<{ worker_alive: boolean; results: SubmitResult[] }> {
  const r = await fetch("/api/tasks", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(profile != null ? { articles, profile } : { articles }),
  });
  if (!r.ok) throw new Error(`/api/tasks → ${r.status} ${await r.text()}`);
  return r.json();
}

export async function createSession(): Promise<number> {
  const r = await fetch("/api/curator/sessions", { method: "POST" });
  if (!r.ok) throw new Error(`create session → ${r.status}`);
  return (await r.json()).session_id;
}

// Parse a free-form textarea (whitespace / commas / newlines) into article tokens.
export function parseArticles(raw: string): string[] {
  return raw.split(/[\s,]+/).map((s) => s.trim()).filter(Boolean);
}
