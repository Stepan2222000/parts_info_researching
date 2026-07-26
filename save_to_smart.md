# Тул `save_to_smart` — спецификация

## Назначение

Атомарная публикация одной или нескольких запчастей в `smart_test` через FDW. Каждая запчасть — либо одиночная деталь, либо kit с компонентами. Бэкенд внутри одной транзакции пишет нужные строки в `smart.parts`, `smart.part_brands`, `smart.part_components` и одновременно одну строку в `parts_research.publications` на каждую успешную публикацию part'а (для трассировки `run_id → smart_id`).

Каждый part в payload — независимая публикация в своём SAVEPOINT. Если один упал — остальные всё равно сохранятся.

## Ограничения Smart-схемы

Тул не валидирует то, что уже форсит БД; правила доступа ниже опираются на эти инварианты:

- **`parts`** — `name` NOT NULL; `articles`, `vehicle_classes`, `model`, `weight_kg`, `description` nullable. `weight_kg > 0 OR NULL`. Колонки `product_type` в `parts` НЕТ (миграция 015) — тип вычисляется из классов во VIEW. `vehicle_classes` — массив слагов (boat/jetski/quad/snowmobile/motorcycle/auto), синхронится в реестр `part_vehicle_classes` триггером; сверенные строки реестра заморожены. Артикулы — regex `^[A-Z0-9\-]{4,20}$`, без дублей внутри массива; для `is_draft=false` минимум один.
- **`part_articles(article PK)`** — глобальный реестр, синхронизируется триггером с `parts.articles[]`. Один артикул не может жить в двух разных `parts.id` (PK violation).
- **`part_brands`** — FK на `brands.name`; при `is_draft=false` любая модификация заблокирована, плюс deferred-требование минимум одного бренда.
- **`part_components`** — PK (parent, child), `quantity > 0`, без self-ref, без циклов; колонки `quantity`, `can_be_sold_separately`. Флаг `is_unverified` живёт на **`parts`** (не на `part_components`) и относится к составу набора, где запчасть — `parent`.
- **`is_draft=false`** замораживает поля `parts` (`name`, `articles`, `weight_kg`, `description`, `model`) и `part_brands`. Состав регулируется отдельно — флагом `is_unverified` на строке parent-`parts`.
- **`is_unverified=false`** (на строке parent-`parts`) замораживает только состав этого набора. Поля `parts` и `part_brands` остаются изменяемыми (при `is_draft=true`).

## Вход

```json
{ "parts": [ { ...part... }, { ...part... } ] }
```

Поля одного `part`:

- `run_id` (обяз.) — id research-run'а, идёт в `publications`.
- `also_run_ids` (опц.) — другие раны ТОЙ ЖЕ физической детали (дубли по другим номерам, сведённые в эту запись из `group` в `get_context`). На каждый пишется отдельная строка `publications` → этот же `smart_id`, чтобы они ушли из очереди. Раны разных деталей сюда класть нельзя.
- `smart_id` (опц.) — id существующей `smart.parts`. **Есть → UPDATE.** Нет → INSERT.
- `name`, `articles`, `vehicle_classes`, `weight_kg`, `model`, `description` — поля `smart.parts`. При INSERT обязательны `name` и непустой `vehicle_classes` (валидация тула). Остальные nullable.
- `name_en`, `description_en` — английские версии (пишутся в `smart.parts_en`). Без `name_en` строка `parts_en` не создаётся (там `name NOT NULL`); `null` == не трогать (как и остальные поля).
- `brands` — массив строк (например `["MERCRUISER"]`). Имена должны быть в `smart.brands`.
- `components` — массив компонентов (если kit). Отсутствие или `[]` — состав не трогаем.
- `part_of` — массив связей «вверх» (эта запчасть — компонент родительских НАБОРОВ): `{smart_id, kit_article, kit_name, quantity}`. Отсутствие или `[]` — связи вверх не трогаем. Семантика — раздел «part_of» ниже.
- `prices` — массив US-офферов за оригинал `{site, price, currency, url, article, in_stock, evidence}`. Пишутся в `parts_prices` через FDW (`market.sites`/`market.observations`) **в той же транзакции/SAVEPOINT, что и публикация part'а** — атомарно: если запись цен упала, весь part (smart + цены) откатывается по своему SAVEPOINT, соседние parts сохраняются. `created_by='parts_research'` проставляется тулом; число записанных — `prices_recorded` в ответе.

Поля одного `component` (run_id наследуется от parent'а):

- `smart_id` (опц.) — id существующей `smart.parts`. Есть → работа с существующей записью, нет → INSERT нового.
- `name`, `articles`, `vehicle_classes`, `weight_kg`, `model`, `description` — те же поля; `vehicle_classes` компонента опционален: пустой/отсутствует → компонент наследует классы родителя-кита.
- `name_en`, `description_en` — английские версии компонента (в `smart.parts_en`). Для нового компонента пишутся при INSERT; для существующего draft-компонента — fill-if-empty (не перетираем уже выставленный EN).
- `brands` — массив строк.
- `quantity` (опц., default 1) — количество в связке `part_components`.

**Семантика `null` и отсутствия ключа.** Тул трактует `field: null` и полное отсутствие ключа в payload одинаково — как «нечего записывать». Значение в БД остаётся прежним (для UPDATE) или не передаётся в INSERT. Тул сам никогда не пишет `NULL` поверх существующего значения. Если курсору нужно реально очистить поле — это делается отдельным `execute_sql`. Это правило одинаково применяется и к parent'у, и к компонентам.

## Правила

### Parent без `smart_id` (создаём новый)

INSERT в `smart.parts` с явными `is_draft=true, is_unverified=true` (FDW не применяет remote DEFAULTs). Все переданные поля как есть. После — INSERT `smart.part_brands` по `brands[]`. Если был `components` — обрабатываем по правилам ниже.

### Parent с `smart_id` (работаем с существующим)

Перед записью backend читает текущее состояние через `SELECT is_draft, is_unverified FROM smart.parts WHERE id=$1`:

1. **Записи нет** → ошибка `smart_id=X not found`.
2. **`is_draft = false`** → отказ всего part'а (Smart-триггеры всё равно заблокируют поля и бренды; модификация состава ещё могла бы пройти, но мы консервативно не разрешаем апдейт published-записи через этот тул).
3. **`is_draft = true`** → разрешён UPDATE:
   - Поля `name`, `articles`, `weight_kg`, `model`, `description` перезаписываются из payload. `vehicle_classes` — merge-only: классы из payload добавляются к существующим, удаление классов тулом невозможно (только человек руками; сверенные строки защищены freeze-триггером).
   - `brands` — если ключ передан: DELETE всех связок + INSERT по payload (включая `brands: []` = очистка).
   - `components` — поведение зависит от `is_unverified`:
     - **`is_unverified = true`**: DELETE всех `part_components` с этим `parent_id` + INSERT новых по `components[]`.
     - **`is_unverified = false`**: если ключа `components` в payload нет — апдейт полей и брендов проходит. Если ключ есть (даже `components: []`) — **отказ всего part'а**: курсор должен либо убрать `components`, либо снять замок (`UPDATE smart.parts SET is_unverified=true ...` через `execute_sql`), либо вызвать `mark_needs_review`.

### Компонент без `smart_id` (создаём новый)

INSERT новой `smart.parts` (явно `is_draft=true, is_unverified=true`) → INSERT его брендов → INSERT связки `part_components(parent_id, child_id, quantity, can_be_sold_separately=false)`. `quantity` по умолчанию 1.

### Компонент с `smart_id` (работаем с существующим)

Backend читает запись компонента:

1. **Записи нет** → ошибка `component smart_id=X not found`.
2. **`is_draft = false`** → саму запись не трогаем. Связка `part_components` с parent'ом создаётся (это разрешено — мы не меняем компонент, только ссылаемся).
3. **`is_draft = true`** → **patch-merge**: записываем поле в `smart.parts` только если в Smart там **пусто**, заполненное не перезаписываем. После — связка.

**Что значит «пусто»:**
- Текстовые поля (`name`, `description`, `model`): `NULL` или `""`.
- `weight_kg`: `NULL`.
- `articles`: `NULL` или пустой массив `'{}'`.
- `brands`: нет ни одной строки `part_brands` с этим `part_id`.

### part_of — связь «вверх» (запчасть входит в набор)

В smart связь «входит в набор» — та же строка `part_components(parent_id, child_id)`, где публикуемая запись — `child_id`. На каждый элемент `part_of` backend в том же SAVEPOINT part'а:

1. **`smart_id` задан** → проверка существования → связка с этим родителем. Родителя бери из `smart_match` элемента `part_of_kits` в `get_context`.
2. **`smart_id` нет, есть `kit_article`** → поиск `SELECT id FROM smart.parts WHERE kit_article = ANY(articles)`. Реестр smart `part_articles` (PK по артикулу) гарантирует максимум одного кандидата:
   - найден → связка с ним;
   - не найден → создаётся **тонкий draft-родитель**: `name = kit_name` (**обязателен**, без него отказ part'а), `articles = [kit_article]`, `vehicle_classes` наследуются от публикуемой записи, `is_draft=true, is_unverified=true`. Запись дозаполнится собственным ресёрчем набора (его `smart_match` найдёт её по номеру).
3. `quantity` — сколько таких деталей в наборе; `null` → 1.

Защиты (сама smart, тексты её ошибок отдаются модели, part откатывается по SAVEPOINT): связка сама с собой (`no_self_reference`), циклы состава (`check_components_cycle`), замороженный состав родителя (`kit_freeze` при `is_unverified=false` — сверенный человеком набор тулом не меняется). Поведение merge-only: существующие upward-связи никогда не удаляются (это состав ЧУЖИХ наборов — в отличие от overwrite собственных `components`); уже существующая связка — no-op (`"linked": "already"`).

**Транзитивный дубль — на кураторе:** если деталь уже достижима от родителя через под-набор (видно в рекурсивном составе `smart_match`), прямую связку НЕ создавать — иначе при разворачивании состава деталь посчитается дважды.

### Порядок `articles` и факты-нюансы (difference-turn) — автоматически

- **Порядок `articles`**: тул выставляет его сам — эталон из `draft_part_articles` run'а (research-порядок «новые→старые»), уточнённый доказанными парами замен `draft_supersession` (новейший номер первым). Куратору выверять порядок в payload не нужно; состав не меняется.
- **Факты-нюансы**: `draft_nuances` всех вошедших в запись ранов (`run_id` + `also_run_ids`) пишутся фактами в базу знаний `part_knowledge` (`knowledge.knowledge_facts`, через FDW) **в той же транзакции/SAVEPOINT, что и публикация part'а** — упала запись фактов → откатывается весь part. Маппинг: нюанс с `articles` → строка на каждый номер (`scope_type='article'`, `scope_ref=<номер>`); без → одна строка на деталь (`scope_type='part'`, `scope_ref=<smart_id>`); `body` = текст + пруф-цитата + URL; `source` = `research:difference_turn:<run_id>`. Одинаковые (scope, body) между ранами группы схлопываются. Перед вставкой прежние research-факты этой детали/её номеров гасятся (`is_active=false`) **по принадлежности** (пере-публикация новым run'ом тоже гасит старое); ручные факты (`source='manual'` и др.) не трогаются. Нюансов нет → база знаний не трогается (пустота может значить упавший difference-turn). Число записанных — `facts_recorded` в ответе.

## Выход

Массив объектов по числу parts, тот же порядок.

```json
[
  {
    "part_index": 0,
    "status": "ok",
    "smart_id": "smart_xxx",
    "components": [
      { "index": 0, "status": "ok", "smart_id": "smart_yyy", "linked": true }
    ],
    "part_of": [
      { "kit_article": "43-883476A3", "smart_id": "smart_zzz", "created_parent": true, "linked": true }
    ]
  }
]
```

В `part_of[*]`: `smart_id` — родитель-набор (найденный или созданный), `created_parent` — создан ли тонкий draft-родитель этим вызовом, `linked` — `true` (связка создана) либо `"already"` (уже существовала, no-op).

Был ли это INSERT или UPDATE — курсор знает по своему payload (если передавал `smart_id` — UPDATE). На каждый успешный part пишется ОДНА строка в `parts_research.publications(run_id, curator_session_id, smart_id, published_by, published_at)`. Поле `action` не пишется (тул всегда означает «применён», без под-настроек).

Машинерия публикации (модели payload, SAVEPOINT-семантика, порядок articles, факты, цены) живёт в общем модуле `src/parts_research/publisher.py`: её делят тул куратора (`published_by='curator'`) и авто-режим воркера (`published_by='auto'`, без сессии; включается `profile.auto_publish`, публикует только однозначный «зелёный коридор» — см. `auto_publish.py`, исход в `task_runs.auto_publish_outcome`).

При ошибке на parent — `components` нет, остальные поля кроме `error` тоже отсутствуют:

```json
{ "part_index": 0, "status": "error",
  "error": "smart_id=smart_13743737 has is_unverified=false; composition is frozen, remove `components` from payload or set is_unverified=true first via execute_sql" }
```

При ошибке внутри компонента весь part откатывается через SAVEPOINT:

```json
{ "part_index": 0, "status": "error",
  "error": "component[1]: smart_id=smart_xxx is finalized — whole part rolled back" }
```

## Примеры

### 1. Новая одиночная запчасть

```json
{ "parts": [ { "run_id": 8, "name": "Сиденье Sea-Doo", "articles": ["295100923"],
  "vehicle_classes": ["jetski"], "weight_kg": 6.8, "brands": ["BRP"] } ] }
```

Backend: INSERT `smart.parts` (с явными `is_draft=true, is_unverified=true`) → INSERT `part_brands(BRP)` → 1 строка в `publications` для этого part'а.

### 2. Новый kit с компонентами без артикулов

```json
{ "parts": [ {
  "run_id": 4, "name": "Kit сальников", "articles": ["76868A04"],
  "vehicle_classes": ["boat"], "brands": ["MERCRUISER"],
  "components": [
    { "name": "Сальники", "articles": [], "brands": ["MERCRUISER"] },
    { "name": "O-rings",  "articles": [], "brands": ["MERCRUISER"] }
  ]
} ] }
```

INSERT parent + его brand → INSERT × 2 component parts + их brands → INSERT × 2 связки `part_components`. Все шаги — в одной транзакции с SAVEPOINT на весь part. Одна строка publications для parent.

### 3. Overwrite существующего kit'а

```json
{ "parts": [ {
  "run_id": 11, "smart_id": "smart_13743737",
  "weight_kg": 0.5, "description": "Уточнено", "brands": ["MERCRUISER"],
  "components": [
    { "smart_id": "smart_08596967", "weight_kg": 0.1 },
    { "smart_id": "smart_93692052" }
  ]
} ] }
```

Backend проверяет `smart_13743737`: `is_draft=true, is_unverified=true` → разрешено. UPDATE parts SET weight_kg, description. DELETE+INSERT brands. DELETE all `part_components` с этим parent_id, INSERT 2 новых. Компонент `smart_08596967`: weight_kg в Smart NULL → запишем 0.1, остальные поля не трогаем. Компонент `smart_93692052`: ничего не передано в payload → саму запись не патчим, только связку.

### 4. Замороженный состав (`is_unverified=false`)

`smart_13743737.is_unverified = false` (состав сверен человеком). Payload **без** `components` проходит — обновятся поля и бренды:

```json
{ "parts": [ { "run_id": 11, "smart_id": "smart_13743737",
  "weight_kg": 0.5, "description": "Уточнено" } ] }
```

Payload с любым `components` (включая `[]`) возвращает:

```json
[ { "part_index": 0, "status": "error",
  "error": "smart_id=smart_13743737 has is_unverified=false; composition is frozen, remove `components` from payload or set is_unverified=true first via execute_sql" } ]
```

### 5. Reuse финализированного компонента в новом kit'е

```json
{ "parts": [ {
  "run_id": 13, "name": "New kit", "articles": ["NK01"],
  "vehicle_classes": ["boat"], "brands": ["MERCRUISER"],
  "components": [ { "smart_id": "smart_36555915", "quantity": 2 } ]
} ] }
```

`smart_36555915` имеет `is_draft=false` → запись не тронули, связка с новым parent создана:

```json
[ { "part_index": 0, "status": "ok", "smart_id": "smart_new",
  "components": [ { "index": 0, "status": "ok", "smart_id": "smart_36555915", "linked": true } ] } ]
```

### 6. Одиночная деталь, входящая в набор (part_of, find-or-create родителя)

```json
{ "parts": [ {
  "run_id": 21, "name": "Подшипник упорный", "articles": ["31-861787"],
  "vehicle_classes": ["boat"], "brands": ["MERCRUISER"],
  "part_of": [ { "kit_article": "43-883476A3", "kit_name": "Верхний редукторный ремкомплект Bravo", "quantity": 2 } ]
} ] }
```

Backend: INSERT детали → поиск `43-883476A3` в `smart.parts.articles`. Нет → INSERT тонкого draft-родителя (`name` = `kit_name`, классы от детали) → INSERT связки `part_components(родитель, деталь, 2)`. Есть → просто связка с существующим. Если родитель уже существует и его состав сверен (`is_unverified=false`) — part падает с текстом freeze-триггера (сверенные составы тул не меняет). Если родитель уже содержит эту деталь через под-набор (видно в `smart_match`) — связь в payload НЕ класть (транзитивный дубль).

### 7. Партия — несколько parts одним вызовом

```json
{ "parts": [
  { "run_id": 6, "name": "Катушка", "articles": ["8M0077471"], "vehicle_classes": ["boat"], "brands": ["MERCRUISER"] },
  { "run_id": 8, "name": "Сиденье", "articles": ["295100923"],  "vehicle_classes": ["jetski"], "brands": ["BRP"] }
] }
```

Каждый part — отдельный SAVEPOINT. Если первый упадёт (например, бренд не в `smart.brands` → FK violation) — второй всё равно сохраняется. В ответе два независимых результата.

## Что курсор должен делать ДО вызова

`smart_id` в payload — решение курсора. Перед `save_to_smart` он обычно через `execute_sql`:

1. По каждому артикулу (parent и компонентов) ищет запись в Smart: `SELECT id, is_draft, is_unverified FROM smart.parts WHERE <article> = ANY(articles)`.
2. Если запись есть и не блокирует нужные изменения — кладёт `smart_id` в payload (overwrite).
3. Если есть и блокирует — обычно `mark_needs_review`, либо снимает замок руками.
4. Если записи нет — payload без `smart_id`, INSERT новой записи.
