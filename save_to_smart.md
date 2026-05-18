# Тул `save_to_smart` — спецификация

## Назначение

Атомарная публикация одной или нескольких запчастей в `smart_test` через FDW. Каждая запчасть — либо одиночная деталь, либо kit с компонентами. Бэкенд внутри одной транзакции пишет нужные строки в `smart.parts`, `smart.part_brands`, `smart.part_components` и одновременно одну строку в `parts_research.publications` на каждую успешную публикацию part'а (для трассировки `run_id → smart_id`).

Каждый part в payload — независимая публикация в своём SAVEPOINT. Если один упал — остальные всё равно сохранятся.

## Вход

```json
{ "parts": [ { ...part... }, { ...part... } ] }
```

Поля одного `part`:

- `run_id` (обяз.) — id research-run'а, идёт в `publications`.
- `smart_id` (опц.) — id существующей `smart.parts`. **Есть → UPDATE.** Нет → INSERT.
- `name`, `articles`, `product_type`, `weight_kg`, `model`, `description` — поля `smart.parts`. `product_type` обязателен при INSERT.
- `brands` — массив строк (например `["MERCRUISER"]`). Имена должны быть в `smart.brands`.
- `components` — массив компонентов (если kit). Отсутствие или `[]` — состав не трогаем.

Поля одного `component` (run_id наследуется от parent'а):

- `smart_id` (опц.) — id существующей `smart.parts`. Есть → работа с существующей записью, нет → INSERT нового.
- `name`, `articles`, `product_type`, `weight_kg`, `model`, `description` — те же поля.
- `brands` — массив строк.
- `quantity` (опц., default 1) — количество в связке `part_components`.

## Правила

### Parent без `smart_id` (создаём новый)

INSERT в `smart.parts` с явными `is_draft=true, is_unverified=true` (FDW не применяет remote DEFAULTs). Все переданные поля как есть. После — INSERT `smart.part_brands` по `brands[]`. Если был `components` — обрабатываем по правилам ниже.

### Parent с `smart_id` (работаем с существующим)

Перед записью backend читает текущее состояние через `SELECT is_draft, is_unverified FROM smart.parts WHERE id=$1`:

1. **Записи нет** → ошибка `smart_id=X not found`.
2. **`is_draft = false`** → отказ всего part'а. В Smart ничего не пишется.
3. **`is_unverified = false`** → отказ всего part'а. Состав замок'нут триггером, и мы консервативно не трогаем и саму запись (чтобы не разъехаться с проверенным человеком составом). Курсор должен снять замок руками (`execute_sql UPDATE smart.parts SET is_unverified=true WHERE id=...`) либо вызвать `mark_needs_review`.
4. **`is_draft = true` И `is_unverified = true`** → разрешён UPDATE:
   - Поля `name`, `articles`, `weight_kg`, `model`, `description` — **перезаписываются** из payload. Что не передано — не трогается (нет UPDATE для отсутствующих ключей).
   - `product_type` — **игнорируется в любом случае** (Smart-триггер `parts_product_type_immutable` запрещает менять после INSERT).
   - `brands` — если ключ передан: DELETE всех существующих связок этого `part_id` + INSERT по payload (полная перезапись, включая `brands: []` = очистка). Если ключа нет в payload — не трогаем.
   - Состав — если `components` передан: DELETE всех `part_components` с этим `parent_id` + INSERT новых по `components[]`. Если `components` не передано — состав вообще не трогаем.

### Компонент без `smart_id` (создаём новый)

INSERT новой `smart.parts` (явно `is_draft=true, is_unverified=true`) → INSERT его брендов → INSERT связки `part_components(parent_id, child_id, quantity, can_be_sold_separately=false)`. `quantity` по умолчанию 1.

### Компонент с `smart_id` (работаем с существующим)

Backend читает запись компонента:

1. **Записи нет** → ошибка `component smart_id=X not found`.
2. **`is_draft = false`** → саму запись не трогаем. Связка `part_components` с parent'ом создаётся (это разрешено — мы не меняем компонент, только ссылаемся).
3. **`is_draft = true`** → **patch-merge**: записываем поле в `smart.parts` только если в Smart там **пусто**. Заполненное — не перезаписываем. `product_type` не трогаем никогда (immutable trigger). После — связка.

**Что значит «пусто»:**
- Текстовые поля (`name`, `description`, `model`): `NULL` или пустая строка `""`. (`name` в реальности NOT NULL, поэтому никогда не пусто; для совместимости проверяем.)
- `weight_kg`: `NULL` (значение `0` НЕ пусто; но в Smart есть CHECK `weight_kg > 0`, так что 0 невозможно).
- `articles`: `NULL` или пустой массив `'{}'`.
- `brands`: нет ни одной строки `part_brands` с этим `part_id` — тогда INSERT всех из payload, иначе не трогаем.

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
    ]
  }
]
```

Был ли это INSERT или UPDATE — курсор знает по своему payload (если передавал `smart_id` — UPDATE). На каждый успешный part пишется ОДНА строка в `parts_research.publications(run_id, curator_session_id, smart_id, published_at)`. Поле `action` не пишется (тул всегда означает «применён», без под-настроек).

При ошибке на parent — `components` нет, остальные поля кроме `error` тоже отсутствуют:

```json
{ "part_index": 0, "status": "error",
  "error": "smart_id=smart_13743737 is verified (is_unverified=false); composition is frozen, set is_unverified=true first via execute_sql" }
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
  "product_type": "Для водного транспорта", "weight_kg": 6.8, "brands": ["BRP"] } ] }
```

Backend: INSERT `smart.parts` (с явными `is_draft=true, is_unverified=true`) → INSERT `part_brands(BRP)` → 1 строка в `publications` для этого part'а.

### 2. Новый kit с компонентами без артикулов

```json
{ "parts": [ {
  "run_id": 4, "name": "Kit сальников", "articles": ["76868A04"],
  "product_type": "Для водного транспорта", "brands": ["MERCRUISER"],
  "components": [
    { "name": "Сальники", "articles": [], "product_type": "Для водного транспорта", "brands": ["MERCRUISER"] },
    { "name": "O-rings",  "articles": [], "product_type": "Для водного транспорта", "brands": ["MERCRUISER"] }
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

### 4. Попытка overwrite замороженного kit'а

`smart_13743737.is_unverified = false` (человек сверил). Любой payload с этим `smart_id` возвращает:

```json
[ { "part_index": 0, "status": "error",
  "error": "smart_id=smart_13743737 is verified (is_unverified=false); composition is frozen, set is_unverified=true first via execute_sql" } ]
```

Курсор обычно дальше делает `mark_needs_review(run_id, 'smart_kit_verified, manual review needed')`.

### 5. Reuse финализированного компонента в новом kit'е

```json
{ "parts": [ {
  "run_id": 13, "name": "New kit", "articles": ["NK01"],
  "product_type": "Для водного транспорта", "brands": ["MERCRUISER"],
  "components": [ { "smart_id": "smart_36555915", "quantity": 2 } ]
} ] }
```

`smart_36555915` имеет `is_draft=false` → запись не тронули, связка с новым parent создана:

```json
[ { "part_index": 0, "status": "ok", "smart_id": "smart_new",
  "components": [ { "index": 0, "status": "ok", "smart_id": "smart_36555915", "linked": true } ] } ]
```

### 6. Партия — несколько parts одним вызовом

```json
{ "parts": [
  { "run_id": 6, "name": "Катушка", "articles": ["8M0077471"], "product_type": "Для водного транспорта", "brands": ["MERCRUISER"] },
  { "run_id": 8, "name": "Сиденье", "articles": ["295100923"],  "product_type": "Для водного транспорта", "brands": ["BRP"] }
] }
```

Каждый part — отдельный SAVEPOINT. Если первый упадёт (например, бренд не в `smart.brands` → FK violation) — второй всё равно сохраняется. В ответе два независимых результата.

## Что курсор должен делать ДО вызова

Передаёт ли он `smart_id` — это решение курсора. Поэтому перед `save_to_smart` курсор обычно делает несколько `execute_sql`:

1. По каждому артикулу из draft — есть ли уже запись в Smart? `SELECT id, is_draft, is_unverified FROM smart.parts WHERE <article> = ANY(articles)`.
2. То же для каждого компонента, у которого есть `article`.
3. Если есть и `is_draft=true, is_unverified=true` — кладёт `smart_id` в payload и формирует overwrite.
4. Если есть и любой флаг блокирующий — обычно `mark_needs_review`, либо снимает замок руками.
5. Если нет — payload без `smart_id`, INSERT новой записи.
