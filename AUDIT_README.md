# AUDIT_README — Руководство для независимого аудита (Phase 2 & Phase 3 Stage 3.1 Foundation)

Документ подготовлен исключительно по фактическому состоянию кода и истории Git для независимой проверки проекта через GitHub.

---

## 1. Назначение проекта

Сервис подбора оптимальных комбинаций цифровых ваучеров пополнения PlayStation Store (регионы `TR`/`US`, валюты `TRY`/`USD`) с расчётом в рублях (копейках), локальным хранением каталога, зашифрованного инвентаря кодов, заказов, платёжных чеков, аудита, попыток доставки и ключей идемпотентности в SQLite.

Продуктовое описание, дорожная карта и архитектурный дизайн Phase 3 зафиксированы в `project.md`, `project_context.md`, `AVITO_TRAFFIC_ENGINE.md` и ветке `docs/phase3-architecture-design` (`PHASE3_ARCHITECTURE_DESIGN.md`).

---

## 2. Архитектура и основные модули

Реализованы базовый вычислительный слой (Phase 1), защищённый слой данных (Phase 2) и фундамент транзакций/ограничений Stage 3.1 (Phase 3 Stage 3.1 Foundation):

- **Phase 1 — Движок оптимального подбора (`src/engine/`, `src/calculator.py`, НЕ ИЗМЕНЯЛСЯ):**
  - `src/engine/models.py` — контракты входных/выходных данных (`Region`, `Currency`, `Denomination`, `SelectedDenomination`, `OptimizationResult`, `ValidationError`, `validate_input_contract`), целочисленные ограничения (`DEFAULT_MAX_VOUCHERS_PER_ORDER = 6`, `MAX_PERMITTED_VOUCHERS_PER_ORDER`).
  - `src/engine/knapsack.py` — `calculate_optimal_vouchers(...)` (Bounded Covering Knapsack без использования float в денежных вычислениях).
  - `src/calculator.py` — публичный фасад экспорта над `src/engine`.
- **Phase 2 + Phase 3 Stage 3.1 Foundation — Слой данных, транзакций и инвариантов (`src/db/`, `src/migrations/`):**
  - `src/db/config.py` — замороженный конфиг `OperationalDefaults` и `DEFAULT_OPERATIONAL_CONFIG` (`OD-08`, `OD-09`, `AR-P3-001`, `AR-P3-006`).
  - `src/db/connection.py` — `get_connection` / `create_connection`, `get_connection_context`, управляемый реестр транзакций по `id(conn)` с проверкой идентичности `entry.conn is conn`, вложенными `SAVEPOINT`, безопасным failure path (`AR-P3-002`), запретом присоединения к неуправляемым/чужим/потоково-несовместимым транзакциям и запретом эскалации режима блокировки; `backup_database`, `restore_database` (с `except BaseException:` очисткой/откатом, `SG-P2-014`).
  - `src/db/uow.py` — `UnitOfWork`, `ReadSession` (строгий `PRAGMA query_only=ON` до `BEGIN DEFERRED`, якорное чтение `schema_migrations` для фиксации WAL read snapshot до передачи управления по `AR-P3-003`, откат на выходе и восстановление `query_only` в outer `finally`), `WriteSession`.
  - `src/db/crypto.py` — `canonicalize_code`, `is_dev_or_test_environment`, `get_encryption_key`, `get_fingerprint_pepper` (минимум 16 байт по `SG-P2-012`), `encrypt_payload`, `decrypt_payload`, `compute_fingerprint` (явный отказ при пустом `key`/`pepper`), `compute_receipt_sha256`.
  - `src/db/migrations.py` — `ensure_migration_table`, `get_applied_migrations`, `discover_migration_files`, `_split_sql_statements` (символьный SQLite-сканер с поддержкой `sqlite3.complete_statement()`, строк, кавычек, `--` и `/* ... */` комментариев, `SG-P2-005`), `MigrationPreflightError`, `_preflight_migration_007`, `run_migrations`.
  - `src/db/models.py` — перечисления статусов (`ProductType`, `ProductStatus`, `DeliveryMethod`, `ApprovalStatus`, `InventoryType`, `InventoryStatus`, `OrderStatus`, `ReceiptReviewStatus`, `UserStatus`, `DeliveryStatus`, `DeliveryKind`, `IdempotencyStatus`), матрица `ALLOWED_ORDER_TRANSITIONS`, `is_inventory_backed_item()`, доменные исключения (`UserBlockedError`, `IdempotencyConflictError`, `IdempotencyInProgressError`, `DeliveryInProgressError`, `DeliveryLeaseExpiredError`, `DeliveryCooldownError`) и dataclass-модели (`AuditEvent`, `DeliveryAttempt`, `DeliveryClaim`, `IdempotencyRecord`, `IdempotencyClaimResult`).
  - `src/db/repositories.py` — транзакционные репозитории (`CatalogRepository`, `UserRepository`, `InventoryRepository`, `OrderRepository`, `ReceiptRepository`, `AuditRepository`, `DeliveryRepository`, `IdempotencyRepository`) и строгий типовой валидатор `_validate_int`.
  - `src/migrations/001_initial_schema.sql`..`007_phase3_integrity_and_foundation.sql` — forward-only SQL-миграции схемы.
  - `src/db/__init__.py` — реэкспорт публичного API пакета `src.db`.

---

## 3. Используемые технологии и зависимости

- **Язык:** Python 3.11+ (локально проверено на `Python 3.14.7`; GitHub Actions `.github/workflows/ci.yml` ориентирован на `3.11` и `3.12` при push/PR в `main`).
- **СУБД:** встроенный `sqlite3` (`SQLite 3.50.4` локально; PRAGMA `foreign_keys = ON`, `journal_mode = WAL`, `synchronous = NORMAL`, `busy_timeout`).
- **Единственная внешняя зависимость (`requirements.txt`):** `cryptography>=41.0.0`.
- **Тестовый фреймворк:** стандартный `unittest`.
- **Сервисы и интерфейсы будущих фаз (НЕ установлены и НЕ реализованы в Stage 3.1):** `aiogram`, `FastAPI`, HTTP-вебхуки, Mini App, реальные воркеры доставки/платежей.

---

## 4. Как установить зависимости

```bash
python -m venv .venv
# Windows PowerShell:
.venv\Scripts\Activate.ps1
# Linux/macOS:
# source .venv/bin/activate

pip install -r requirements.txt
```

---

## 5. Как запустить тесты

```bash
python -m unittest discover -v tests
```

Фактический результат на локальном окружении (`Python 3.14.7`, `SQLite 3.50.4`): `Ran 181 tests ... OK` (включая `147` базовых/адаптированных тестов Phase 2, `1` race-тест `SG-P2-007` в `tests/test_inventory.py` и `33` новых Stage 3.1 + remediation тестов в `tests/test_stage31_*.py`).
*Примечание:* локально установлен только Python 3.14.7 (`py -0p`); прогон на Python 3.11 и 3.12 требует запуска через CI.

---

## 6. Как создать тестовую базу

Для программного создания и инициализации локальной SQLite БД используется `create_connection` (`src/db/connection.py`):

```bash
python -c "from src.db import create_connection, run_migrations; conn = create_connection('data/test_audit.db'); run_migrations(conn); conn.close()"
```

По умолчанию при `db_path=None` используется путь `DEFAULT_DB_PATH = Path("data/ps_service.db")`.

---

## 7. Как применяются миграции

Механизм миграций реализован в `src/db/migrations.py`:

1. `discover_migration_files()` сканирует `src/migrations/*.sql` по шаблону `^(\d+)_(.*)\.sql$` и сортирует по числовому номеру версии.
2. `ensure_migration_table()` создаёт таблицу `schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum TEXT, applied_at TIMESTAMP)`.
3. Для каждой неприменённой миграции `run_migrations()` открывает транзакцию `with transaction(conn, mode="IMMEDIATE"):`, после получения write-блокировки проверяет `schema_migrations` и SHA-256 `checksum`, запускает pre-flight хуки внутри этой же транзакции (`MIGRATION_PREFLIGHT_CHECKS`), разбивает SQL-скрипт через `_split_sql_statements()` и записывает версию в `schema_migrations` внутри той же транзакции, обеспечивая атомарный rollback при любой ошибке.

Применяемые forward-only миграции:
- `001_initial_schema.sql` .. `006_order_items_update_and_product_type_guards.sql` — исторические миграции Phase 2 (**не редактировались, SHA-256 checksums неизменны**).
- `007_phase3_integrity_and_foundation.sql` — Stage 3.1 БД-инварианты и новые таблицы:
  - 9 pre-flight проверок (`_preflight_migration_007` с `LEFT JOIN` для обнаружения сиротских строк, отсутствующих/некорректных detail rows, смешанных заказов `OD-04`, `CANCELLED+RESERVED`, двусторонних инвариантов `COMPLETED`, множественных `ACCEPTED` чеков и некорректных forensic-ссылок `AR-P3-007`).
  - Backfill `orders.paid_at` для legacy `PAID`/`FULFILLING`/`COMPLETED` заказов перед установкой триггеров `AR-P3-005`.
  - Индексы `idx_receipts_single_accepted_per_order`, `idx_orders_status_created_at`, `idx_inventory_order_status`.
  - Триггеры целостности `voucher_details`, `fixed_product_details`, `voucher_denominations`, `order_items` (включая запрет смешивания `MANUAL_REVIEW` и inventory-backed позиций `OD-04` и строгую заморозку `INSERT`/`UPDATE`/`DELETE` для `order_items` при любом статусе заказа кроме `PENDING_PAYMENT`, `SG-P31-002`).
  - Триггеры `inventory_items`: терминальность `SOLD` (`OD-06`, с сохранением возможности ротации `fingerprint` и `updated_at`), запрет прямой вставки `SOLD`, запрет `AVAILABLE -> SOLD`, семантика `QUARANTINED`/`DISABLED` и forensic-ссылок (`AR-P3-007`), парный контроль `INSERT`/`UPDATE` квот резервирования.
  - Триггеры `orders` и `payment_receipts` (`AR-P3-005`, `SG-P2-004`) + append-only защита `audit_events`.
  - Таблицы `order_deliveries` (с append-only триггерами, запретом non-NULL `error_code` в статусе `SENT` и уникальным индексом активной попытки `DISPATCHING`, `AR-P3-001`, `SG-P31-002`) и `idempotency_keys` (`CHECK`-инвариант обязательных `response_entity_type` и `response_entity_id >= 1` для `COMPLETED`, `AR-P3-006`, `SG-P31-003`).

---

## 8. Архитектурная логика и инварианты Stage 3.1

- **Координация транзакций и `UnitOfWork` (`src/db/connection.py`, `src/db/uow.py`):**
  - Вложенные вызовы `with transaction(conn, mode="IMMEDIATE"):` используют `SAVEPOINT` поверх единой корневой транзакции; отслеживается флаг `savepoint_created`, чтобы `ROLLBACK TO` не вызывался на несозданном savepoint, а ошибки cleanup никогда не маскировали исходное исключение (`AR-P3-002`).
  - `UnitOfWork.read()` выставляет `PRAGMA query_only = ON` до `BEGIN DEFERRED`, фиксирует WAL read snapshot реальным запросом `SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1;` (а не константным `SELECT 1`, `AR-P3-003`), при отсутствии/пустоте `schema_migrations` выбрасывает `DatabaseError("Database schema is not initialized...")`, освобождает snapshot через `rollback()` и всегда восстанавливает предыдущий `query_only` в outer `finally`.
- **Продуктовые решения (`OD-01`–`OD-09`):**
  - `OD-01` / `OD-02`: `BLOCKED` пользователи блокируются на серверной стороне (`UserBlockedError`) при создании заказов, загрузке чеков, получении `get_revealed_code()` и `DeliveryRepository.claim_delivery()`. Неоплаченные заказы могут быть атомарно отменены с возвратом `RESERVED` инвентаря в `AVAILABLE`.
  - `OD-03` / `OD-04`: `OFFICIAL_ACTIVATION` работает с `InventoryType.ACTIVATION_DATA`; разрешены совместные заказы `VOUCHER` + inventory-backed `FIXED_PRODUCT`, а смешивание с `MANUAL_REVIEW` запрещено и в Python (`OrderRepository.create_order`, `InventoryRepository.finalize_order_sale`), и SQL-триггерами `007`.
  - `OD-05`: Пользователь не может отменить заказ в `PAYMENT_REVIEW`; `OrderRepository.cancel_order_atomically()` при отмене оператором атомарно освобождает `RESERVED` инвентарь, переводит ожидающие `PENDING_REVIEW` чеки в `REJECTED` и записывает пару событий аудита (`ORDER_STATUS_TRANSITION` как основное событие перехода + `ORDER_CANCELLED_ATOMIC` со сводкой побочных эффектов). Stage 3.2 `OrderService.cancel_order()` обязан делегировать в этот метод без создания третьего дублирующего audit-события (`SG-P31-005`).
  - `OD-06` / `OD-07`: `SOLD` терминален и защищён триггером от отката или подмены payload/product/order; операторский интерфейс в Stage 3.1 не имеет break-glass просмотра plaintext и использует примитивы слепой доставки.
  - `OD-08` / `OD-09`: Пороги (`3` открытых заказа, `900s` TTL, `60s` lease, `20.0s` min send budget при `15.0s` HTTP timeout + `5.0s` safety margin, `60s` redelivery cooldown) сосредоточены в `OperationalDefaults`; `voucher_denominations.cost_rub_kopecks` трактуется как розничная цена без изменения Phase 1.
- **Семантика доставки (`DeliveryRepository`, `AR-P3-001`, `SG-P31-001`, `SG-P31-002`, `SG-P31-004`):**
  - Система честно моделирует **at-least-once delivery**: каждая попытка создаёт отдельную append-only запись с уникальными `delivery_id`, `attempt_no`, `claim_token`; все timestamp-поля DTO (`DeliveryClaim`, `DeliveryAttempt`, `IdempotencyRecord`) возвращаются как timezone-aware UTC `datetime`. Перед внешним HTTP-вызовом `check_pre_send_lease_budget()` внутри транзакции сверяет живую запись `(delivery_id, claim_token)` в БД, проверяет статус `DISPATCHING` (отказ `DELIVERY_PRE_SEND_REJECTED_STALE_STATE` при `UNCERTAIN`/чужом токене), оценивает `min(mono_remaining, utc_remaining)` с учётом safety margin (`>= max(min_send_budget_seconds, http_timeout_seconds + safety_margin_seconds)`) и переводит в `UNCERTAIN (LEASE_EXPIRED_BEFORE_SEND)` только условным `UPDATE` для активного владельца.
  - Поздний `SENT` (`UNCERTAIN -> SENT`) сохраняет `external_message_id` и `delivered_at`, очищает `error_code = NULL`, фиксирует прежнюю ошибку в аудите `DELIVERY_LATE_SENT_AFTER_RECLAIM` и не затрагивает новую попытку или `SOLD` инвентарь; устаревший `FAILED` игнорируется (`DELIVERY_STALE_OUTCOME_IGNORED`). Дублирование доставки при сетевом таймауте или длительной паузе процесса после pre-send проверки математически исключить нельзя, и `exactly-once` НЕ заявляется.
- **Семантика идемпотентности (`IdempotencyRepository`, `AR-P3-006`, `SG-P31-003`):**
  - `claim()` атомарно фиксирует `(scope, actor_key, idempotency_key)` в статусе `IN_PROGRESS` с `owner_token`, возвращает `REPLAY` при `COMPLETED` и совпадающем `request_hash`, отклоняет конфликт хеша (`IdempotencyConflictError`), допускает reclaim просроченного `IN_PROGRESS` после `stale_after` (старый `owner_token` при этом отклоняется) и выселяет/очищает истёкшие ключи (`expires_at`). SQLite не ограничивает фиксацию `IN_PROGRESS` на уровне commit-time `CHECK`, однако табличный `CHECK` в `007` и `mark_completed()` гарантируют, что `COMPLETED` всегда имеет непустой `response_entity_type` и положительный целочисленный `response_entity_id >= 1` (с явным отклонением `bool`).

---

## 9. Где реализованы криптография, хранение секретов и управление ключами

Вся криптографическая логика находится в `src/db/crypto.py` (`Fernet`, `hashlib`, `hmac`). См. регламент эксплуатации ключей и ротации:
- `INVENTORY_ENCRYPTION_KEY` (32 url-safe base64 байта Fernet) и `FINGERPRINT_PEPPER` (минимум 16 байт по `SG-P2-012`) обязательны вне явно помеченных `dev`/`test` сред (`is_dev_or_test_environment()`).
- Явная передача пустых `key=b""` или `pepper=b""` немедленно отклоняется с `CryptoConfigurationError` без отката к переменным окружения (`SG-P2-012`).
- `AuditRepository` рекурсивно отклоняет передачу секретных ключей (`plaintext`, `code`, `raw_code`, `claim_token`, `owner_token`, `encryption_key`, `pepper`, `secret`, `password`) в `audit_events`.
- Ротация `FINGERPRINT_PEPPER` (`rotate_fingerprint_pepper(new_pepper, old_pepper=...)`) проверяет `old_pepper` до обновления (`SG-P2-011`) и корректно обновляет `fingerprint` даже на терминальных `SOLD` позициях.

---

## 10. Какие функции намеренно НЕ реализованы (Stage 3.2+)

- Сервисный слой оркестрации (`src/services/`: checkout, receipt workflow, fulfillment & Telegram sender workers, cron/TTL reaper).
- Миграция `008` (если потребуется в Stage 3.2 после утверждения Stage 3.1).
- Telegram-бот (`aiogram 3.x`), FSM, хендлеры команд и операторские кнопки.
- Mini App (Frontend), FastAPI Backend, HTTP-вебхуки платёжных провайдеров, RBAC/админка.
- Парсер PS Store и автоферма аккаунтов (Playwright).

---

## 11. Известные ограничения

1. **SQLite single-writer:** все транзакции записи сериализуются в пределах `timeout_seconds`.
2. **Операционный регламент `restore_database()`:** обязательная полная остановка всех процессов приложения и закрытие соединений до восстановления.
3. **Операционный регламент `rotate_fingerprint_pepper()`:** требует остановки писателей (`quiesce`) и синхронной выкатки нового `FINGERPRINT_PEPPER`.
4. **At-least-once delivery (`AR-P3-001`):** редкие дубликаты сообщений возможны при сетевом сбое/паузе процесса после pre-send проверки.
5. **Версии Python в CI vs локально:** локально протестировано на `Python 3.14.7` (`181 tests OK`); прогон матрицы `Python 3.11` / `3.12` (`.github/workflows/ci.yml`) остаётся задачей CI.

---

## 12. Действующие ограничения этапа

В рамках Stage 3.1 ветка `feat/phase3-stage31-foundation` передаётся на независимое Argon Gate Review. До явного решения Product Commander запрещены переход к Stage 3.2, модификация Phase 1 (`src/engine/`, `src/calculator.py`), изменение исторических миграций `001`–`006`, мерж в `main` и открытие PR.

---

## 13. История этапов в Git

- **Phase 2 Verified Baseline (`a261edcd2154e524426d6a258da1862c92ce297d`):** `Merge pull request #1 from HipTwinse/security/phase2-remediation` (`main`, `origin/main`).
- **Phase 3 Architecture Design Revision 2 (`71025e6f2b114d1df6eb23c5810fbddafeb2401c`):** ветка `docs/phase3-architecture-design`.
- **Phase 3 Stage 3.1 Foundation (`feat/phase3-stage31-foundation`):** реализация фундамента Stage 3.1, миграции `007`, исправлений `AR-P3-001`–`008`, `SG-P2-005`–`016`, `OD-01`–`09` и remediation-замечаний `SG-P31-001`–`005` (`34` новых детерминированных теста: `1` `SG-P2-007` + `33` Stage 3.1 / remediation).
