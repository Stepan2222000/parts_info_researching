// Types + client fetchers. The browser talks only to same-origin Next route
// handlers under /api/*, which proxy to the Python FastAPI process server-side.

export type RunStatus =
  | "queued" | "running" | "done"
  | "failed_no_data" | "failed_validation" | "failed_crashed"
  | "needs_human_review";

export const STATUS_ORDER: RunStatus[] = [
  "queued", "running", "done", "needs_human_review",
  "failed_no_data", "failed_validation", "failed_crashed",
];

export const STATUS_LABEL: Record<string, string> = {
  queued: "В очереди",
  running: "В работе",
  done: "Готово",
  needs_human_review: "Нужен человек",
  failed_no_data: "Нет данных",
  failed_validation: "Ошибка валидации",
  failed_crashed: "Сбой",
};

// Терминальные статусы — на них доступен ретрай (повторный submit → новый run).
export const TERMINAL_STATUSES: RunStatus[] = [
  "done", "needs_human_review", "failed_no_data", "failed_validation", "failed_crashed",
];
export const isTerminal = (s: string): boolean => (TERMINAL_STATUSES as string[]).includes(s);

export interface TaskCard {
  task_id: number;
  article: string;
  run_id: number;
  status: RunStatus;
  error: string | null;
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
  result_json: unknown;
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
export const getSessions = () => jget<{ sessions: SessionSummary[] }>("/api/curator/sessions");

export async function submitArticles(articles: string[]): Promise<{ worker_alive: boolean; results: SubmitResult[] }> {
  const r = await fetch("/api/tasks", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ articles }),
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
