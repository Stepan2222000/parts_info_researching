# Public Research API — для внешних процессов

Публичный HTTP-API, через который **внешние программы (в т.ч. на других серверах)**
ставят артикулы в общую очередь ресерча и **синхронно дожидаются результата**.

Поднимается отдельным процессом `python -m parts_research.cli.public_api`
(в Docker — сервис `public-api`, порт `8100`). Содержит **только** ресерч:
куратора и UI-выборок тут нет.

- **Без аутентификации** — обращаться может кто угодно из доступной сети.
- **Дедуп по артикулу**: повторный submit артикула, у которого уже есть активный
  (`queued`/`running`) run, вернёт тот же `run_id` с `reused: true` — ретраи не
  плодят лишних прогонов.
- **Нужен живой воркер.** Сам API только кладёт в очередь; обрабатывает воркер
  (`cli.worker`). Если живого воркера нет — запрос не виснет, а сразу вернёт
  `worker_alive: false`.

База: `http://<host>:8100` (порт настраивается `PARTS_RESEARCH_PUBLIC_API_PORT`).

---

## `POST /research` — поставить и дождаться

Тело:
```json
{ "articles": ["817373A1", "865496A01"] }
```

Блокируется до готовности **всех** артикулов или до потолка ожидания
(по умолчанию **600 сек**, см. `PARTS_RESEARCH_WAIT_TIMEOUT`). Ответ:

```json
{
  "worker_alive": true,
  "results": [
    {
      "article": "817373A1",
      "task_id": 42,
      "run_id": 64,
      "reused": false,
      "status": "done",
      "result_json": { "...": "StructuredResult — итоговый JSON по запчасти" },
      "error": null,
      "needs_review_reason": null,
      "worker_alive": true,
      "timed_out": false
    }
  ]
}
```

Поля `entry`:

| поле | смысл |
|---|---|
| `article` | нормализованный артикул (`.strip().upper()`) либо исходный, если невалиден |
| `task_id` / `run_id` | идентификаторы задачи и прогона (`null`, если `invalid`/`refused`) |
| `reused` | `true` — переиспользован уже активный run (дедуп) |
| `status` | см. таблицу статусов ниже |
| `result_json` | итоговый JSON по запчасти (`StructuredResult`) или `null`, если ещё не готов / не было данных |
| `error` | текст ошибки (для `failed_*` / `invalid` / `refused`); иначе `null` |
| `needs_review_reason` | причина для `needs_human_review` (иначе `null`) |
| `worker_alive` | был ли живой воркер |
| `timed_out` | `true` — упёрлись в потолок ожидания, результат ещё не финальный (дозапроси `GET /research/{run_id}`) |

Статусы:

| status | значение |
|---|---|
| `done` | успех, `result_json` заполнен |
| `needs_human_review` | собрано, но требует человека (причина в `needs_review_reason`) |
| `failed_no_data` | артикул не найден в источниках |
| `failed_validation` | модель отдала невалидный результат |
| `failed_crashed` | крэш прогона (трейс в `error`) |
| `queued` / `running` | ещё в работе (бывает при `timed_out` или отсутствии воркера) |
| `invalid` | артикул не прошёл валидацию (`^[A-Z0-9\-]+$`) |
| `refused` | артикул уже финализирован человеком в Smart — ресерч пропущен |

---

## `GET /research/{run_id}` — дозапросить по run_id

Для `timed_out`-случая или асинхронного использования (получил `run_id` из
`POST /research`, дальше опрашиваешь). Возвращает один `entry` (без батч-обёртки).
`404`, если run не найден.

---

## `GET /health` — жив ли сервис

```json
{ "ok": true, "worker_alive": true, "live_workers": 1, "queued": 3 }
```

Чтобы понять, что «всё живо»: ответ на HTTP = API up; `worker_alive`/`live_workers`
= есть ли кто обрабатывает; `queued` = глубина очереди.

---

## Примеры

**curl (подождать результат):**
```bash
curl -sS -X POST http://HOST:8100/research \
  -H 'Content-Type: application/json' \
  -d '{"articles":["817373A1"]}'
```

**Python (httpx):**
```python
import httpx

BASE = "http://HOST:8100"

# здоровье системы
print(httpx.get(f"{BASE}/health", timeout=10).json())

# submit-and-wait (таймаут клиента ставь ≥ серверного потолка, т.е. > 600с)
r = httpx.post(f"{BASE}/research", json={"articles": ["817373A1", "865496A01"]}, timeout=700)
for e in r.json()["results"]:
    if e["status"] == "done":
        print(e["article"], "->", e["result_json"])
    elif e["timed_out"]:
        # ещё не готово — дозапросим позже
        later = httpx.get(f"{BASE}/research/{e['run_id']}", timeout=10).json()
        print(e["article"], "still", later["status"])
    else:
        print(e["article"], e["status"], e["error"])
```

> Совет: клиентский таймаут HTTP должен быть **больше** серверного потолка
> ожидания (`PARTS_RESEARCH_WAIT_TIMEOUT`, по умолчанию 600с), иначе клиент
> оборвётся раньше сервера. Прогон сам по себе обычно 1–3 мин.
