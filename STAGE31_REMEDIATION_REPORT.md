# STAGE 3.1 FOUNDATION — REMEDIATION REPORT (`SG-P31-001`–`SG-P31-005`)

Точечное устранение замечаний предварительного вердикта **APPROVE WITH CONDITIONS** для Phase 3 Stage 3.1 Foundation.

---

## 1. Исходный и итоговый Commit SHA

- **Repository:** `HipTwinse/ps-service`
- **Production baseline (`main` & `origin/main`):** `a261edcd2154e524426d6a258da1862c92ce297d` (tree `94789428ab8017f854dfa4678df0dfbdbb1fb9cd`) — **не изменён**
- **Рабочая ветка:** `feat/phase3-stage31-foundation`
- **Исходный commit до remediation:** `a84eb46f39b376960f9ed708754568d8ea52098c`
- **Итоговый commit SHA:** фиксируется локальным коммитом в ветке `feat/phase3-stage31-foundation` сразу после завершения финальной верификации (точный SHA приводится в итоговом ответе Product Commander; push/PR/merge не выполнялись).

---

## 2. Список изменённых файлов

### Код и миграции (`src/`)
- `src/db/config.py` — добавлен `delivery_safety_margin_seconds: float = 5.0`, обновлено значение `delivery_min_send_budget_seconds: float = 20.0`, добавлены инварианты валидации в `OperationalDefaults.__post_init__`.
- `src/db/models.py` — добавлено поле `safety_margin_seconds: float = 5.0` в `DeliveryClaim`, сохранены строгие `datetime` / `Optional[datetime]` контракты на `DeliveryClaim`, `DeliveryAttempt`, `IdempotencyRecord`.
- `src/migrations/007_phase3_integrity_and_foundation.sql` — обновлена напрямую (до попадания в `main`): заморозка `order_items` при любом статусе заказа кроме `PENDING_PAYMENT`, запрет `SENT` с ненулевым `error_code` на `order_deliveries`, табличный `CHECK` для `COMPLETED` в `idempotency_keys`. Миграции `001`–`006` не изменялись.
- `src/db/repositories.py` — реализованы UTC/повторные DB-проверки в `DeliveryRepository.check_pre_send_lease_budget()` и `claim_delivery()`, очистка `error_code = NULL` при `UNCERTAIN -> SENT` и `DISPATCHING -> SENT` в `record_outcome()`, обязательные валидируемые поля в `IdempotencyRepository.mark_completed()`, парсинг SQLite timestamp в timezone-aware UTC `datetime` (`_ensure_utc`, `_parse_utc_ts`, `_parse_optional_utc_ts`, удалён `# type: ignore[arg-type]`), и явный контракт аудита в `OrderRepository.cancel_order_atomically()`.

### Тесты и документация
- `tests/test_phase2_security_gate_remediation.py` — обновлено утверждение для `PAID` заказа (`order_sold`) в соответствии с заморозкой `order_items` вне `PENDING_PAYMENT`.
- `tests/test_stage31_migration_007_and_guards.py` — добавлены `test_clean_install_001_to_007_and_idempotency`, `test_sg_p31_002_order_items_frozen_outside_pending_payment_and_delivery_error_code_cleared`, `test_sg_p31_003_idempotency_keys_sql_check_enforces_completed_metadata`.
- `tests/test_stage31_remediations_and_primitives.py` — обновлены assertions по дефолтам `20.0 / 15.0 / 5.0` и `late SENT`, добавлены `test_sg_p31_001_*`, `test_sg_p31_003_*`, `test_sg_p31_004_*`, `test_sg_p31_005_*`.
- `AUDIT_README.md` — обновлены контракты Stage 3.1, defaults и ожидаемое число тестов (`181`).
- `STAGE31_IMPLEMENTATION_REPORT.md` — обновлён SHA-256 `007` и итоговая статистика тестов (`181`).
- `STAGE31_REMEDIATION_REPORT.md` *(новый)* — текущий отчёт об устранении `SG-P31-001`–`SG-P31-005`.

---

## 3. `SG-P31-001` — Delivery lease budget

- **Исправление (`src/db/config.py`, `src/db/models.py`, `src/db/repositories.py`):**
  - В `OperationalDefaults` зафиксированы согласованные defaults: `delivery_lease_seconds = 60`, `delivery_http_timeout_seconds = 15.0`, `delivery_safety_margin_seconds = 5.0`, `delivery_min_send_budget_seconds = 20.0`. В `__post_init__` и `DeliveryRepository.claim_delivery()` запрещены конфигурации, где `min_send_budget_seconds < http_timeout_seconds + safety_margin_seconds`, или превышается `lease_seconds`, а также отклоняются `bool`, `<= 0` и `NaN`/`inf`.
  - В `DeliveryRepository.check_pre_send_lease_budget()` внутри `with transaction(self.conn, mode="IMMEDIATE"):`:
    1. Сверяет живую строку `order_deliveries` по `(id = claim.delivery_id, claim_token = claim.claim_token)`.
    2. Если строка отсутствует или её статус уже не `DISPATCHING` (например, переведена в `UNCERTAIN` конкурентным reaper), HTTP-вызов запрещается (`DeliveryLeaseExpiredError`), а текущее состояние не перезаписывается (при stale status пишется аудит `DELIVERY_PRE_SEND_REJECTED_STALE_STATE`).
    3. Проверяет как UTC-остаток `(lease_expires_at - ref_now)`, так и monotonic-остаток `(lease_monotonic_deadline - current_mono)`: если `utc_remaining <= 0`, `mono_remaining <= 0`, или `min(mono_remaining, utc_remaining) < max(min_send_budget_seconds, http_timeout_seconds + safety_margin_seconds)`, условно обновляет запись `WHERE id = ? AND claim_token = ? AND status = 'DISPATCHING'` в `UNCERTAIN` (`LEASE_EXPIRED_BEFORE_SEND`), логирует `LEASE_EXPIRED_BEFORE_SEND` и поднимает `DeliveryLeaseExpiredError`.
    4. При достаточном бюджете возвращает ограниченный HTTP timeout `min(claim.http_timeout_seconds, effective_remaining - claim.safety_margin_seconds)`.
  - Политика честности сохранена: `exactly-once` не заявляется; проверки перед отправкой и safety margin снижают вероятность дублирующей отправки, но не исключают её при паузе процесса или сетевом разрыве во время HTTP-вызова.
- **Тесты (`tests/test_stage31_remediations_and_primitives.py`):**
  - `test_sg_p31_001_pre_send_lease_budget_db_checks_margin_and_concurrent_reaper` и `test_od_02_03_04_05_08_policies_and_defaults` покрывают: истечение по UTC и monotonic до pre-send, статус `UNCERTAIN` до pre-send, устаревший `claim_token`, случай `18.0s > 15.0s`, но `< 15.0s + 5.0s`, конкурентный `reap_expired_leases` между `claim_delivery` и `check_pre_send_lease_budget`, и согласованность с не-UTC timezone-aware временем.

---

## 4. `SG-P31-002` — Order item freeze и late `SENT`

- **Исправление 2.1 (`src/migrations/007_phase3_integrity_and_foundation.sql`):**
  - Триггеры `trg_order_items_freeze_non_pending_insert`, `trg_order_items_freeze_non_pending_update`, `trg_order_items_freeze_non_pending_delete` запрещают любое `INSERT`, `UPDATE`, `DELETE` над `order_items`, если заказ находится в любом статусе, отличном от `PENDING_PAYMENT`.
  - В `trg_order_items_freeze_non_pending_update` проверяются оба заказа (`OLD.order_id` и `NEW.order_id`), полностью закрывая перенос позиции между `PENDING_PAYMENT` и non-`PENDING_PAYMENT` заказами.
- **Исправление 2.2 (`src/db/repositories.py`, `007_phase3_integrity_and_foundation.sql`):**
  - В `DeliveryRepository.record_outcome()` при переходе `UNCERTAIN -> SENT` выставляется `status = 'SENT', external_message_id = ?, error_code = NULL, delivered_at = ?, updated_at = ?`, прежняя `row["error_code"]` сохраняется в audit-событии `DELIVERY_LATE_SENT_AFTER_RECLAIM`, более новая попытка доставки не затрагивается; также `DISPATCHING -> SENT` очищает `error_code = NULL`.
  - В `trg_order_deliveries_protect_immutable_fields` добавлена SQL-блокировка `NEW.status = 'SENT' AND NEW.error_code IS NOT NULL`.
- **Тесты:**
  - `tests/test_stage31_migration_007_and_guards.py`: `test_sg_p31_002_order_items_frozen_outside_pending_payment_and_delivery_error_code_cleared` (проверка `INSERT`/`UPDATE`/смены `order_id` в обе стороны/`DELETE` для всех 6 статусов `PAYMENT_REVIEW`, `PAID`, `FULFILLING`, `COMPLETED`, `REFUND_REVIEW`, `CANCELLED` + прямой SQL на `SENT` с non-NULL `error_code`).
  - `tests/test_phase2_security_gate_remediation.py`: обновлена проверка строки 1130 для `PAID` заказа.
  - `tests/test_stage31_remediations_and_primitives.py`: в `test_ar_p3_001_*` проверено `res_late.error_code is None`, сохранение `previous_error_code == "LEASE_EXPIRED_BEFORE_SEND"` в аудите и сохранность истории обеих попыток.

---

## 5. `SG-P31-003` — Idempotency `COMPLETED` invariant

- **Исправление (`src/migrations/007_phase3_integrity_and_foundation.sql`, `src/db/repositories.py`):**
  - В таблицу `idempotency_keys` добавлен табличный `CHECK`: `status = 'IN_PROGRESS'` допускает `NULL` в `response_entity_*`, тогда как `status = 'COMPLETED'` требует `response_entity_type IS NOT NULL AND TRIM(response_entity_type) != '' AND response_entity_id IS NOT NULL AND typeof(response_entity_id) = 'integer' AND response_entity_id >= 1`.
  - В `IdempotencyRepository.mark_completed()` аргументы `response_entity_type: str` и `response_entity_id: int` сделаны обязательными keyword-only параметрами с валидацией непустой строки и строгого положительного целого `_validate_int(..., min_value=1)` (явно отклоняет `bool`, `None`, `0`, отрицательные числа, `float`, `str`) до изменения строки; сохраняется проверка `owner_token` и `status = 'IN_PROGRESS'`.
  - Честно фиксируется: данный SQL `CHECK` гарантирует корректность `COMPLETED` записей и не претендует на запрет фиксации состояния `IN_PROGRESS`.
- **Тесты:**
  - `tests/test_stage31_remediations_and_primitives.py`: `test_sg_p31_003_idempotency_mark_completed_validation_and_replay` (отсутствие аргументов, `None`, `""`/`"   "`, `id=0`, `-5`, `True`/`False` `bool`, некорректные типы, сохранение `IN_PROGRESS` и успешный `COMPLETED` + `REPLAY`).
  - `tests/test_stage31_migration_007_and_guards.py`: `test_sg_p31_003_idempotency_keys_sql_check_enforces_completed_metadata` (прямые SQL `INSERT`/`UPDATE` проверки).

---

## 6. `SG-P31-004` — Timestamp DTO types

- **Исправление (`src/db/models.py`, `src/db/repositories.py`):**
  - Выбран предпочтительный вариант: единый контракт **timezone-aware UTC `datetime`** (и `Optional[datetime]` для `DeliveryAttempt.delivered_at`) во всех полях `DeliveryClaim`, `DeliveryAttempt` и `IdempotencyRecord`.
  - Добавлены `_ensure_utc()`, `_parse_utc_ts()`, `_parse_optional_utc_ts()`; строки SQLite парсятся в UTC `datetime(tzinfo=timezone.utc)`, а входные non-UTC timezone-aware `datetime` нормализуются в UTC.
  - Полностью удалён единственный `# type: ignore[arg-type]` в `src/` (ранее в `claim_delivery`).
- **Тесты (`tests/test_stage31_remediations_and_primitives.py`):**
  - `test_sg_p31_004_utc_timezone_aware_dto_timestamp_contracts` проверяет фактические типы `isinstance(..., datetime)`, `tzinfo == timezone.utc`, `utcoffset() == timedelta(0)` на всех timestamp-полях `DeliveryClaim`, `DeliveryAttempt` и `IdempotencyRecord`, а также корректную UTC-нормализацию при входе с `+04:00`.

---

## 7. `SG-P31-005` — Duplicate audit events (вывод и контракт для Stage 3.2)

- **Анализ `OrderRepository.cancel_order_atomically()`:**
  - Метод вызывает `transition_order_status()`, создающий `ORDER_STATUS_TRANSITION` (`from_status`, `to_status='CANCELLED'`, `notes`, actor metadata), а затем фиксирует `ORDER_CANCELLED_ATOMIC` (`from_status`, `released_inventory_count`, `rejected_pending_receipts`, `notes`).
- **Зафиксированный контракт:**
  - Оба события в рамках атомарной транзакции отмены имеют различную семантику и признаны допустимыми и обязательными на текущем этапе:
    - **`ORDER_STATUS_TRANSITION`** — **основное** каноническое событие жизненного цикла статуса заказа.
    - **`ORDER_CANCELLED_ATOMIC`** — обязательное summary-событие побочных эффектов отмены (количество освобождённого инвентаря и отклонённых чеков).
  - **Требование для Stage 3.2:** `OrderService.cancel_order()` **ОБЯЗАН** делегировать отмену в `OrderRepository.cancel_order_atomically()` и **НЕ ДОЛЖЕН** записывать третье дублирующее audit-событие отмены заказа.
- **Тест:** `test_sg_p31_005_cancel_order_atomically_audit_contract` подтверждает ровно два события после перехода в `PAYMENT_REVIEW`: `["ORDER_STATUS_TRANSITION", "ORDER_CANCELLED_ATOMIC"]`.

---

## 8. Полный результат тестов

После всех правок кода, тестов и документации запущен полный набор:
- Команда: `python -m unittest discover -v tests`
- Результат: **181 tests run, 0 failures, 0 errors, 0 skipped — OK** (включая clean install `001`–`007`, upgrade `001`–`006` -> `007`, 9 проверок rollback pre-flight `007`, прямые SQL bypass-тесты и все регрессионные тесты `SG-P31-001`–`005`).

---

## 9. Версии Python и SQLite

- **Локально проверено:** `Python 3.14.7` (`SQLite 3.50.4`).
- **Python 3.11 / 3.12 (CI):** локально не установлены (`py -0p` показывает только `-V:3.14`). Прогон CI (`.github/workflows/ci.yml` для матриц `3.11` и `3.12`) на ветке `main` / PR пока **не выполнялся**, так как push и открытие PR запрещены без отдельного разрешения Product Commander.

---

## 10. Migration Checksums (SHA-256, raw bytes)

| Версия | Файл миграции | SHA-256 | Статус |
|---|---|---|---|
| `001` | `001_initial_schema.sql` | `1039037a63b203b270be65d0325b4950814b60d946ccea9d3a80237ea5fa517d` | Неизменна (baseline) |
| `002` | `002_provider_scoped_receipts.sql` | `7ea9dd850ef69789d1943568948dfa3f0a94d4926c5cefac5cb3d1e92338c417` | Неизменна (baseline) |
| `003` | `003_inventory_and_integrity_guards.sql` | `f81494473a244566503f59ed21f6a0f3abe2de0117eb65bb205c9f4f78b764a6` | Неизменна (baseline) |
| `004` | `004_order_item_denomination.sql` | `beac060c3b3fa6f7f8ba8f9b5359d096d93a4d089a8c74627cfef1127c450f5a` | Неизменна (baseline) |
| `005` | `005_inventory_order_delete_restrict.sql` | `1c157e36c888897e5626ab19816659aca7c710fc2ea93539626a277a72b94f72` | Неизменна (baseline) |
| `006` | `006_order_items_update_and_product_type_guards.sql` | `9ac4528903fd9e00128791710ea346f58935e65c229cddc70e95949cd8d7c92e` | Неизменна (baseline) |
| `007` | `007_phase3_integrity_and_foundation.sql` | `6beaf34991ab1fa3d59f22aefd823e602ac53ef1a4c5b6b68b4d3e645ae3b921` | Обновлена (`SG-P31-002`/`003`) |

---

## 11. Подтверждение неизменности Phase 1 и миграций `001`–`006`

- `git diff a261edcd2154e524426d6a258da1862c92ce297d..HEAD -- src/engine/ src/calculator.py requirements.txt src/migrations/001_*.sql src/migrations/002_*.sql src/migrations/003_*.sql src/migrations/004_*.sql src/migrations/005_*.sql src/migrations/006_*.sql` — **пустой вывод (diff = 0)**.
- `src/services/`, `src/bot/`, миграция `008` — **не создавались**.

---

## 12. Оставшиеся риски и ограничения

1. **Python 3.11 / 3.12 CI:** совместимость с Python 3.11 и 3.12 требует запуска CI после получения разрешения на push/PR.
2. **At-least-once delivery:** `check_pre_send_lease_budget()` + `delivery_safety_margin_seconds` (`5.0s`) уменьшают риск параллельной доставки, но не исключают дубликат при длительной паузе процесса (GC/OS stall) или зависании сети уже после начала внешнего HTTP-вызова.
3. **Синхронность `now_utc` и `now_monotonic` в `DeliveryRepository`:** в обычной работе оба параметра не передаются и используют единое текущее время системы; при инъекции синтетического `now_utc` в `claim_delivery()` вызывающий код должен передавать согласованный `now_utc` в `check_pre_send_lease_budget()`, иначе сверка DB `lease_expires_at` сочтёт запись истекшей по UTC.
4. **Этап Stage 3.2 (сервисы, Telegram-бот, RBAC):** намеренно не начат и ожидает решения Product Commander.
