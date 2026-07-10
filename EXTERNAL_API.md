# Public Research API — для внешних процессов

Публичный HTTP-API, через который **внешние программы (в т.ч. на других серверах)**
ставят артикулы в общую очередь ресерча и получают результат — **синхронно**
(submit-and-wait) либо **прогрессивно** (submit + поллинг турнов с курсором:
статусы, какая фаза идёт, и растущий JSON по мере готовности этапов).

Поднимается отдельным процессом `python -m parts_research.cli.public_api`
(в Docker — сервис `public-api`, порт `8100`). Содержит **только** ресерч:
куратора и UI-выборок тут нет.

Базовые URL:
- `https://parts-research-api.parts-everything.site` (домен, TLS через Caddy)
- `http://<host>:8100` (голый порт, для внутрисерверных потребителей)

- **Без аутентификации** — обращаться может кто угодно из доступной сети.
- **Нужен живой воркер.** Сам API только кладёт в очередь; обрабатывает воркер
  (`cli.worker`). Если живого воркера нет — запрос не виснет, а сразу вернёт
  `worker_alive: false`.

---

## Профили этапов

Пайплайн рана: `main → family_expansion → low_confidence → kit_contents →
price_fallback → difference → phase2`. Ядро (`main`, `kit_contents`, валидации)
не отключается; остальное выбирается профилем. **Набор без состава — критический
фейл** (`failed_validation: kit without contents`), поэтому kit_contents всегда включён.

Поле `profile` в submit-запросах — имя пресета или явный список этапов:

| пресет | этапы сверх ядра | время |
|---|---|---|
| `fast` | — | ~2–3 мин |
| `default` (по умолчанию) | family_expansion, low_confidence, price_fallback, difference | ~5–7 мин |
| `full` | default + phase2 (свободный агентский добор) | ~8–10 мин |

Кастомный набор: `{"stages": ["family_expansion", "difference"]}` (перечисляются
только опциональные этапы). Профиль фиксируется на ране и виден во всех ответах.

**Исход каждого этапа** отдаётся в `stage_outcomes`: `ok` | `pending` | `running` |
`not_applicable` (условие не сработало: например, деталь не набор) |
`skipped_by_profile` | `failed: <текст>` (best-effort этап упал — данные этапа
отсутствуют из-за сбоя, а не потому, что их нет в природе; можно ретраить force'ом).

## Семантика reuse / force

- **Активный ран** артикула (`queued`/`running`) всегда переиспользуется:
  `reused: true` + его `run_id` и `profile`; `covers_requested` показывает,
  покрывает ли его профиль запрошенные этапы. `force` активный ран **не**
  дублирует (`force_ignored: true`) — дождись и форси потом.
- **Готовый `done`-ран**, чей профиль покрывает запрошенный, отдаётся сразу:
  `is_final: true` + `result_json`, новый ран не ставится и деньги не тратятся.
  Принудительный пере-ресерч — `force: true`.
- **`refused`** (артикул финализирован человеком в Smart): ресерч не запускается,
  но к отказу прикладывается `last_run` с последним раном, если он был.

---

## `POST /research/submit` — поставить и не ждать (основной путь для программ)

```json
{ "articles": ["817373A1", "865496A01"], "profile": "default", "force": false }
```

Мгновенный ответ по каждому артикулу:

```json
{
  "worker_alive": true,
  "requested_profile": {"preset": "default", "stages": ["family_expansion", "low_confidence", "price_fallback", "difference"]},
  "results": [
    {
      "article": "817373A1",
      "task_id": 42, "run_id": 64,
      "reused": false, "profile": {"preset": "default", "stages": ["..."]},
      "covers_requested": true, "force_ignored": false,
      "status": "queued", "is_final": false,
      "result_json": null, "error": null, "needs_review_reason": null,
      "worker_alive": true, "timed_out": false
    }
  ]
}
```

Особые случаи: `status: "invalid"` (артикул не прошёл `^[A-Z0-9\-]+$`),
`"refused"` (+ `last_run`), готовый ран → `status: "done", is_final: true,
result_json: {...}` сразу.

Дальше — поллинг `/research/{run_id}/turns`.

## `GET /research/{run_id}/turns?since=N&mode=delta|snapshot` — прогрессивная выдача

Курсор `since` — номер последнего виденного турна (`latest_turn` из прошлого
ответа; первый запрос — `since=0`). Поллить раз в 5–10 сек.

```json
{
  "run_id": 64, "article": "817373A1", "status": "running", "is_final": false,
  "profile": {"preset": "default", "stages": ["..."]},
  "stage_outcomes": {"main": "ok", "family_expansion": "running", "phase2": "skipped_by_profile", "...": "..."},
  "progress": {"current_stage": "family_expansion", "turns_done": 1, "queue_position": null},
  "latest_turn": 1,
  "legacy_run": false,
  "turns": [
    {"turn_idx": 1, "stage": "main", "status": "ok",
     "started_at": "...", "finished_at": "...", "duration_s": 172.4, "error": null,
     "summary": "+2 в confirmed (803100T1, 8M0095485); +7 состав набора; заполнено: название"}
  ],
  "delta": {
    "changed": { "name": "…", "numbers.article": [ "...полные секции..." ], "kit_contents": [ "..." ] },
    "summary": "+2 в confirmed (…); +7 состав набора; заполнено: название"
  },
  "snapshot": null
}
```

- `mode=delta` (дефолт): `delta.changed` — **только изменившиеся секции** JSON
  (верхнеуровневые поля; `numbers` разбит на `numbers.article` /
  `numbers.article_low_confidence` / `numbers.irrelevant`) от состояния на турне
  `since` к текущему, **целиком новое значение секции**. Слияние у потребителя —
  простая замена одноимённых секций. Отставший курсор получает одну слитую
  дельту, а не пачку. Пустая дельта (`changed: {}`) = ничего не изменилось.
- `mode=snapshot`: полный текущий JSON в `snapshot` (вход для потерявших курсор).
- `turns` — новые турны после `since`, включая **упавшие** (с `error`); у ok-турнов
  есть `summary` — русская сводка «что нового» (пригодна для промпта агента).
- Важно: данные **не монотонны** — difference-turn может переклассифицировать
  номер `confirmed → irrelevant`; это видно в дельте и проговаривается в summary.
- Курсор следующего запроса = `latest_turn`. Это номер последнего **завершённого**
  турна (running-турн виден в `turns`, но курсор не двигает — его снапшота ещё нет).
  `since > latest_turn` → HTTP 400.
- `legacy_run: true` — ран старше механики турнов: `turns` пуст, снапшот —
  финальный `result_json`.

Типовой цикл потребителя:

```python
import httpx, time

BASE = "https://parts-research-api.parts-everything.site"
TERMINAL = {"done", "needs_human_review", "failed_no_data",
            "failed_validation", "failed_crashed", "skipped_smart_approved"}

r = httpx.post(f"{BASE}/research/submit",
               json={"articles": ["817373A1"], "profile": "default"}, timeout=30).json()
entry = r["results"][0]
if entry["is_final"]:                      # готовый ран переиспользован
    print(entry["result_json"]); raise SystemExit
run_id, cursor, state = entry["run_id"], 0, {}

while True:
    d = httpx.get(f"{BASE}/research/{run_id}/turns",
                  params={"since": cursor, "mode": "delta"}, timeout=30).json()
    if d["latest_turn"] > cursor and d["delta"]:
        cursor = d["latest_turn"]
        state.update(d["delta"]["changed"])          # merge = замена секций
        print("новое:", d["delta"]["summary"])       # можно скормить агенту
    if d["status"] in TERMINAL:
        print("итог:", d["status"], d.get("error"))
        break
    time.sleep(7)
```

## `GET /research/{run_id}` — статус/результат одним запросом

Как раньше (`status`, `result_json`, `error`, `needs_review_reason`), плюс
`profile`, `is_final`, `stage_outcomes` и `progress {current_stage, turns_done,
queue_position}`. У неготового рана `result_json` — **промежуточный** (растёт по
турнам). `404`, если run не найден.

## `GET /research/by-article/{article}` — вернуться к задаче без run_id

Последний ран артикула: `{article, task_id, run_id, status, is_final, profile,
error}`. `404` — ранов не было.

## `POST /research` — поставить и дождаться (синхронный, совместимый)

Тело то же, что у `/research/submit` (`articles` + опц. `profile`, `force`),
плюс `"wait": false` — не ждать: поставить и сразу вернуть `run_id` с текущим
статусом (для клиентов за NAT/файрволами, режущими молчащие соединения, —
эквивалент `/research/submit`). По умолчанию блокируется до готовности **всех**
артикулов или до потолка ожидания (по умолчанию **1200 сек**, см.
`PARTS_RESEARCH_WAIT_TIMEOUT`). Формат entry — как в `/research/submit` +
`timed_out`. Клиентский HTTP-таймаут ставь **больше** серверного потолка.
`timed_out: true` → дозапрашивай `GET /research/{run_id}` или поллинг `/turns`.

## `GET /health` — жив ли сервис

```json
{ "ok": true, "worker_alive": true, "live_workers": 1, "queued": 3 }
```

---

## Статусы

| status | значение |
|---|---|
| `done` | успех, `result_json` финален |
| `needs_human_review` | собрано, но требует человека (причина в `needs_review_reason`) |
| `failed_no_data` | артикул не найден в источниках |
| `failed_validation` | невалидный результат; в т.ч. `kit without contents` — набор без состава |
| `failed_crashed` | крэш прогона (трейс в `error`) |
| `queued` / `running` | ещё в работе; прогресс — в `/turns` |
| `invalid` | артикул не прошёл валидацию (`^[A-Z0-9\-]+$`) |
| `refused` | финализирован человеком в Smart — ресерч пропущен (+ `last_run`) |
| `skipped_smart_approved` | состав в Smart уже сверен человеком — ресерч не нужен |
