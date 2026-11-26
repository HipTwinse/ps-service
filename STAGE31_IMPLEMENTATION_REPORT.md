# STAGE 3.1 FOUNDATION — IMPLEMENTATION REPORT (FOR ARGON GATE REVIEW)

Статус: **Реализация Stage 3.1 Foundation завершена и готова к независимому Security & Architecture Gate Review (Argon).**
Stage 3.2 намеренно не начат. Мерж в `main` и открытие PR не выполнялись.

---

## 1. Baseline и рабочая ветка

- **Repository:** `HipTwinse/ps-service`
- **Production baseline (`main` & `origin/main`):** `a261edcd2154e524426d6a258da1862c92ce297d` (tree: `94789428ab8017f854dfa4678df0dfbdbb1fb9cd`) — **не изменён**
- **Architecture Revision 2 (`docs/phase3-architecture-design`):** `71025e6f2b114d1df6eb23c5810fbddafeb2401c`
- **Рабочая ветка:** `feat/phase3-stage31-foundation` (ответвлена от `main @ a261edcd2154e524426d6a258da1862c92ce297d`)
- **Commit SHA:** фиксируется отдельным коммитом на ветке `feat/phase3-stage31-foundation` после прохождения финальной верификации (см. сопроводительный отчёт Product Commander; `main` остаётся на `a261edcd2154e524426d6a258da1862c92ce297d`).

---

## 2. Список изменённых и добавленных файлов

### Производственный код и схема БД
- `src/db/config.py` *(новый)* — `OperationalDefaults`, `DEFAULT_OPERATIONAL_CONFIG` (`OD-08`, `OD-09`).
- `src/db/models.py` *(изменён)* — `DeliveryStatus`, `DeliveryKind`, `IdempotencyStatus`, `is_inventory_backed_item()`, `UserBlockedError(ValidationError)`, `IdempotencyConflictError`, `IdempotencyInProgressError`, `DeliveryInProgressError`, `DeliveryLeaseExpiredError`, `DeliveryCooldownError`, dataclass-модели `DeliveryAttempt`, `DeliveryClaim`, `IdempotencyRecord`, `IdempotencyClaimResult`.
- `src/db/connection.py` *(изменён)* — управляемый реестр транзакций по `id(conn)` с проверкой `entry.conn is conn` и `get_active_managed_transaction_count()`, вложенные `SAVEPOINT`, `AR-P3-002` failure-safe обработка создания/cleanup savepoint, `read_only`/lock-mode/thread-affinity проверки, `SG-P2-014` `except BaseException:` в `restore_database()`.
- `src/db/uow.py` *(новый)* — `UnitOfWork`, `ReadSession` (`AR-P3-003` `PRAGMA query_only=ON`, `BEGIN DEFERRED`, якорное чтение `schema_migrations`, восстановление `query_only` в outer `finally`), `WriteSession`.
- `src/db/crypto.py` *(изменён)* — `SG-P2-012` валидация минимальной длины `FINGERPRINT_PEPPER` (16 байт), пропуск whitespace-only env-переменных, запрет пустого `key`/`pepper` в `_resolve_cipher()` и `compute_fingerprint()`.
- `src/db/migrations.py` *(изменён)* — `SG-P2-005` quote/comment-aware `_split_sql_statements()`, `MigrationPreflightError`, `_preflight_migration_007()` (9 проверок с `LEFT JOIN`), `MIGRATION_PREFLIGHT_CHECKS`, атомарное выполнение pre-flight + DDL + `schema_migrations` в `BEGIN IMMEDIATE`.
- `src/migrations/007_phase3_integrity_and_foundation.sql` *(новый, forward-only)* — индексы, backfill legacy `orders.paid_at`, DB-триггеры (`voucher_details`, `fixed_product_details`, `voucher_denominations`, `order_items`, `inventory_items`, `orders`, `payment_receipts`, `audit_events`) и новые таблицы `order_deliveries`, `idempotency_keys` со своими триггерами.
- `src/db/repositories.py` *(изменён)* — новые `AuditRepository`, `DeliveryRepository` (`AR-P3-001`), `IdempotencyRepository` (`AR-P3-006`); обновления `CatalogRepository`, `UserRepository` (`AR-P3-008`, `SG-P2-010`, `SG-P2-016`), `InventoryRepository` (`SG-P2-011`, `SG-P2-013`, `OD-01`, `OD-03`, `AR-P3-007`), `OrderRepository` (`OD-01`..`OD-08`, `SG-P2-004`, `SG-P2-009`, `SG-P2-014`, `SG-P2-015`), `ReceiptRepository` (`OD-02`, `OD-05`, `AR-P3-005`, `SG-P2-014`, `SG-P2-016`).
- `src/db/__init__.py` *(изменён)* — экспорт всех новых классов, функций и исключений Stage 3.1.

### Тесты и документация
- `tests/test_inventory.py` *(изменён)* — добавлен конкурентный race-тест последнего доступного кода `test_concurrent_reserve_items_for_order_last_unit_race` (`SG-P2-007`, `Barrier(2)`).
- `tests/test_orders.py` *(изменён)* — адаптация под `OD-04` (разделение совместного `VOUCHER+OFFICIAL_ACTIVATION` и отдельного `MANUAL_REVIEW` заказа) и `SG-P2-004` (финализация инвентаря перед `COMPLETED`); удалён `time.sleep`.
- `tests/test_phase2_audit_remediation.py`, `tests/test_phase2_remediation_followup.py`, `tests/test_phase2_security_gate_remediation.py` *(изменены)* — актуализация ожидаемого списка миграций (`1..7`), приведение `order_a` в `PAID` перед тестом SQL cap на `SOLD`, явная проверка блокировки `CANCELLED` до освобождения резервов, замена `time.sleep(0.15)` на детерминированный `set_trace_callback` `Event`.
- `tests/test_stage31_transactions_and_uow.py` *(новый, 9 тестов)* — реестр транзакций, вложенные `SAVEPOINT`, `AR-P3-002` failure injection, границы транзакций, `AR-P3-003` WAL snapshot positive & `SELECT 1` negative controls, восстановление `query_only`.
- `tests/test_stage31_migration_007_and_guards.py` *(новый, 7 тестов)* — сверка SHA-256 `001`–`006`, upgrade `001..006 -> 007`, идиома legacy `paid_at`, все 9 pre-flight откатов `AR-P3-004`, прямые SQL bypass-тесты (`OD-04`, `OD-06`, `AR-P3-001`, `AR-P3-005`, `AR-P3-007`, `audit_events`).
- `tests/test_stage31_remediations_and_primitives.py` *(новый, 10 тестов)* — `SG-P2-005`..`016`, `OD-01`..`08`, `AR-P3-001`, `AR-P3-006`, `AR-P3-008`, конкурентные гонки `claim_delivery` и `IdempotencyRepository.claim` на `Barrier(2)`.
- `AUDIT_README.md` *(обновлён)*, `STAGE31_IMPLEMENTATION_REPORT.md` *(новый)*.

---

## 3. Изменения миграций и проверка SHA-256 Checksums

Миграции `001`–`006` **не изменялись**. Добавлена forward-only миграция `007_phase3_integrity_and_foundation.sql`.

| Версия | Файл миграции | SHA-256 (raw bytes) | Статус |
|---|---|---|---|
| `001` | `001_initial_schema.sql` | `1039037a63b203b270be65d0325b4950814b60d946ccea9d3a80237ea5fa517d` | Неизменна (baseline) |
| `002` | `002_provider_scoped_receipts.sql` | `7ea9dd850ef69789d1943568948dfa3f0a94d4926c5cefac5cb3d1e92338c417` | Неизменна (baseline) |
| `003` | `003_inventory_and_integrity_guards.sql` | `f81494473a244566503f59ed21f6a0f3abe2de0117eb65bb205c9f4f78b764a6` | Неизменна (baseline) |
| `004` | `004_order_item_denomination.sql` | `beac060c3b3fa6f7f8ba8f9b5359d096d93a4d089a8c74627cfef1127c450f5a` | Неизменна (baseline) |
| `005` | `005_inventory_order_delete_restrict.sql` | `1c157e36c888897e5626ab19816659aca7c710fc2ea93539626a277a72b94f72` | Неизменна (baseline) |
| `006` | `006_order_items_update_and_product_type_guards.sql` | `9ac4528903fd9e00128791710ea346f58935e65c229cddc70e95949cd8d7c92e` | Неизменна (baseline) |
| `007` | `007_phase3_integrity_and_foundation.sql` | `6beaf34991ab1fa3d59f22aefd823e602ac53ef1a4c5b6b68b4d3e645ae3b921` | Новая (Stage 3.1 + Remediation SG-P31-002/003) |

*Примечание о line endings:* для `002`–`006` baseline checksums соответствуют сырым байтам файлов из репозитория; для `007` файл записан с `LF`, поэтому `read_bytes()` и UTF-8 текст дают идентичный хеш `6beaf349...`.

---

## 4. Результаты тестирования по версиям Python

- **Python 3.14.7 (SQLite 3.50.4, локально):**
  - Команда: `python -m unittest discover -v tests`
  - Результат (с учётом remediation `SG-P31-001`–`005`, см. `STAGE31_REMEDIATION_REPORT.md`): **181 tests run, 0 failures, 0 errors, 0 skipped (OK)**.
- **Python 3.11 и Python 3.12:**
  - Статус: **НЕ ЗАПУСКАЛИСЬ ЛОКАЛЬНО** (`py -0p` подтверждает наличие только `Python 3.14` на данной Windows-машине).
  - Инфраструктура: `.github/workflows/ci.yml` настроен на матрицу `["3.11", "3.12"]` для ветки `main`; подтверждение на `3.11`/`3.12` должно быть получено через GitHub Actions или соответствующее окружение перед мержем. Локальный прогон на `3.14.7` **не** выдаётся за прохождение CI на `3.11`/`3.12`.

---

## 5. Результаты Security & Regression Tests

Целевые прогоны модулей безопасности:
- `tests/test_stage31_transactions_and_uow.py`: **9 / 9 OK** (`AR-P3-002`, `AR-P3-003`, `SG-P2-004`).
- `tests/test_stage31_migration_007_and_guards.py`: **10 / 10 OK** (`AR-P3-004`, `AR-P3-005`, `AR-P3-007`, `OD-04`, `OD-06`, `SG-P2-006`, `SG-P31-002`, `SG-P31-003`, clean install & SQL bypass-тесты).
- `tests/test_stage31_remediations_and_primitives.py`: **14 / 14 OK** (`SG-P2-005`..`016`, `OD-01`..`08`, `AR-P3-001`, `AR-P3-006`, `AR-P3-008`, `SG-P31-001`, `SG-P31-003`, `SG-P31-004`, `SG-P31-005`).
- `tests/test_phase2_security_gate_remediation.py`: **17 / 17 OK**.
- Все остальные наборы (`test_engine.py`, `test_db_schema.py`, `test_crypto*.py`, `test_backup.py`, `test_restore_safety.py`, `test_inventory.py`, `test_orders.py`, `test_receipts*.py`, `test_migrations_remediation.py`, `test_phase2_*.py`): включены в общий прогон **181 / 181 OK**.

---

## 6. Матрица AR-P3-001–008 (Stage 3.1 Acceptance Criteria)

| ID | Приоритет | Статус в Stage 3.1 | Доказательства (код, БД и тесты) |
|---|---|---|---|
| `AR-P3-001` | HIGH | **CLOSED** *(в объёме Stage 3.1 persistence & lease primitives; Stage 3.2 HTTP worker — вне scope)* | Таблица и триггеры `order_deliveries` (`007`), `DeliveryRepository` (`claim_delivery`, `check_pre_send_lease_budget`, `record_outcome`, `reap_expired_leases`), append-only попытки, monotonic timeout budget (`LEASE_EXPIRED_BEFORE_SEND`), `DELIVERY_LATE_SENT_AFTER_RECLAIM`, `DELIVERY_STALE_OUTCOME_IGNORED`, honest at-least-once документация в `AUDIT_README.md`. Тесты: `test_ar_p3_001_delivery_lease_budget_reclaim_late_sent_stale_failed_and_cooldown`, `test_ar_p3_001_concurrent_claim_delivery_race`, `test_direct_sql_payment_order_audit_delivery_and_catalog_guards`. |
| `AR-P3-002` | MEDIUM | **CLOSED** | `src/db/connection.py` (`transaction`): учёт `savepoint_created` до инкремента `depth`, запрет `ROLLBACK TO`/`RELEASE` при провале `SAVEPOINT`, независимая cleanup-обработка в `except BaseException`, проверки `conn.in_transaction`. Тесты (`_FaultyConnection` failure injection): `test_ar_p3_002_savepoint_creation_failure_does_not_rollback_nonexistent_savepoint`, `test_ar_p3_002_savepoint_cleanup_error_never_masks_original_exception`, `test_ar_p3_002_savepoint_release_failure_cleans_up_depth_and_rolls_back`. |
| `AR-P3-003` | MEDIUM | **CLOSED** | `src/db/uow.py` (`UnitOfWork.read`): `PRAGMA query_only=ON` до `BEGIN DEFERRED`, якорный запрос `SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1;`, явная `DatabaseError("Database schema is not initialized...")`, rollback и восстановление `query_only` в outer `finally`. Тесты: `test_ar_p3_003_uow_read_wal_snapshot_positive_and_select1_negative_control`, `test_ar_p3_003_uow_read_rejects_writes_and_restores_query_only_and_checks_schema`. |
| `AR-P3-004` | MEDIUM | **CLOSED** | `src/db/migrations.py` (`_preflight_migration_007`, 9 `LEFT JOIN`/detail/mixed-order/cancelled/two-sided completed/`ACCEPTED`/quarantine проверок в одной транзакции `BEGIN IMMEDIATE` перед DDL). Тесты: `test_ar_p3_004_preflight_*` (все 9 проверок с подтверждённым полным rollback схемы и версии). |
| `AR-P3-005` | MEDIUM | **CLOSED** | `007` SQL backfill `paid_at`, индекс `idx_receipts_single_accepted_per_order`, триггеры `trg_orders_validate_insert_paid_at`, `trg_orders_validate_status_and_payment_update`, `trg_payment_receipts_*`; проверки в `OrderRepository.transition_order_status` & `ReceiptRepository.update_review_status`. Тесты: `test_baseline_001_006_checksums_and_007_upgrade_and_legacy_paid_at_backfill`, `test_direct_sql_payment_order_audit_delivery_and_catalog_guards`. |
| `AR-P3-006` | LOW | **CLOSED** *(Stage 3.1 persistence primitives)* | `007` таблица `idempotency_keys` + `IdempotencyRepository` (`claim`, `mark_completed`, `release_in_progress`, `cleanup_expired`); документировано отсутствие SQLite commit-time CHECK на `IN_PROGRESS`. Тесты: `test_ar_p3_006_idempotency_lifecycle_replay_conflict_stale_reclaim_and_cleanup`, `test_ar_p3_006_concurrent_idempotency_claim_race`. |
| `AR-P3-007` | LOW | **CLOSED** | `007` триггеры `trg_inventory_items_prevent_invalid_quarantine_insert` и `trg_inventory_items_validate_status_transition`; `InventoryRepository` (`quarantine_item`, `disable_item`, `clear_forensic_order_link`, `restore_to_available`). Тесты: `test_ar_p3_004_preflight_completed_invariants_multiple_accepted_and_quarantine` (кейс 9), `test_direct_sql_sold_immutability_and_quarantine_guards`. |
| `AR-P3-008` | LOW | **CLOSED** | Аудит чтений в `repositories.py` (все write-методы возвращают DTO через `self.get_*` до выхода из `with transaction()`), `UserRepository.get_or_create_user` перечитывает пользователя после `UPDATE username` внутри транзакции; `restore_database` защищён `BaseException`. Тест: `test_sg_p2_014_restore_base_exception_and_ar_p3_008_user_updated_at`. |

---

## 7. Матрица SG-P2-005–016

| ID | Статус | Доказательства исправления и проверки |
|---|---|---|
| `SG-P2-005` | **CLOSED** | Quote/comment-aware `_split_sql_statements()` в `src/db/migrations.py` (`sqlite3.complete_statement()`, multi-stmt строки, `--`/`/* */`, quotes/identifiers). Тест: `test_sg_p2_005_split_sql_statements_edge_cases`. |
| `SG-P2-006` | **CLOSED** | Миграция `007`: триггеры деталей продуктов, `voucher_denominations`, `inventory_items` (`RESERVED`/`SOLD` квоты и запрет переназначения). Тесты: `test_direct_sql_*` + `tests/test_phase2_security_gate_remediation.py`. |
| `SG-P2-007` | **CLOSED** | Заменены `time.sleep` в `test_orders.py` и `test_phase2_remediation_followup.py` на `set_trace_callback` `Event`; добавлен race-тест `test_concurrent_reserve_items_for_order_last_unit_race` (`Barrier(2)`, `is_alive() == False`) в `test_inventory.py`. |
| `SG-P2-008` | **CLOSED** | `CatalogRepository.add_voucher_denomination()` разделяет `ValidationError` (несоответствие `voucher_details` / `region`/`currency`) и `ConflictError` (`UNIQUE`); дублируется триггерами `007`. Тест: `test_sg_p2_008_009_010_015_016_catalog_user_order_receipt_audit_validations`. |
| `SG-P2-009` | **CLOSED** | `OrderRepository.create_order()` строго валидирует `isinstance(..., str)` и допустимые значения `currency` для заказа и позиций; регрессионно подтверждено `test_sg_p2_008_009_010_015_016_...` и `test_sg_p2_003_rejects_invalid_types_and_currencies`. |
| `SG-P2-010` | **CLOSED** | `CatalogRepository.update_fixed_product_approval()` и `UserRepository.update_user_status()` проверяют `cur.rowcount == 0` и поднимают `NotFoundError`. Тест: `test_sg_p2_008_009_010_015_016_...`. |
| `SG-P2-011` | **CLOSED** | `InventoryRepository.rotate_fingerprint_pepper(new_pepper, old_pepper=...)` проверяет текущий отпечаток против `old_pepper` до обновления с откатом всей транзакции при несоответствии. Тест: `test_sg_p2_011_and_012_crypto_pepper_rotation_and_empty_key_guards`. |
| `SG-P2-012` | **CLOSED** | `src/db/crypto.py`: `get_fingerprint_pepper()` требует >= 16 байт и пропускает пробельные значения; `_resolve_cipher()` и `compute_fingerprint()` отклоняют явный `b""` с `CryptoConfigurationError`. Тест: `test_sg_p2_011_and_012_crypto_pepper_rotation_and_empty_key_guards`. |
| `SG-P2-013` | **CLOSED** | `InventoryRepository.get_revealed_code()` требует `expected_order_id` и `expected_user_id`, в одном `JOIN` проверяет `users.status` (`UserBlockedError`), и выдаёт нейтральные anti-oracle сообщения. Тест: `test_sg_p2_013_and_od_01_reveal_requires_context_blocks_blocked_user_and_neutral_errors`. |
| `SG-P2-014` | **CLOSED** | `restore_database()` (`connection.py`) ловит `BaseException` при staging/swapping/rollback; write-репозитории читают DTO внутри транзакции до `commit`. Тест: `test_sg_p2_014_restore_base_exception_and_ar_p3_008_user_updated_at`. |
| `SG-P2-015` | **CLOSED** | `OrderRepository.transition_order_status()` всегда записывает JSON (`from_status`, `to_status`, `notes`, actor metadata) в `audit_events`, даже когда `notes is None`. Тест: `test_sg_p2_008_009_010_015_016_...`. |
| `SG-P2-016` | **CLOSED** | Унифицирована строгая проверка `isinstance(..., str)` и Enum типов в `CatalogRepository`, `UserRepository`, `ReceiptRepository`, `AuditRepository`. Тест: `test_sg_p2_008_009_010_015_016_...`. |

*(Также закрыто исходное корневое замечание `SG-P2-004` — координация транзакций через `UnitOfWork`/`SAVEPOINT` и синхронизация статусов заказа/инвентаря в Python и SQL-триггерах `007`.)*

---

## 8. Проверка Phase 1 (`src/engine/`, `src/calculator.py`) diff = 0

Выполнена проверка относительно baseline `a261edcd2154e524426d6a258da1862c92ce297d`:
`git diff a261edcd2154e524426d6a258da1862c92ce297d -- src/engine/ src/calculator.py requirements.txt src/migrations/001..006` вернул **пустой вывод (diff = 0)**.

---

## 9. Известные ограничения и остаточные риски

1. **Python 3.11 / 3.12 CI:** локально проверен только `Python 3.14.7`; совместимость с Python 3.11 и 3.12 должна быть подтверждена запуском `.github/workflows/ci.yml`.
2. **At-least-once delivery:** архитектурно при падении или длительной паузе воркера между `check_pre_send_lease_budget()` и завершением сетевого запроса Telegram возможна редкая дублирующая отправка (честно документировано; `DELIVERY_LATE_SENT_AFTER_RECLAIM` фиксирует факт в аудите).
3. **Требования к legacy БД при переходе на 007:** перед применением `007` на существующей базе любые заранее повреждённые строки (осиротевшие ссылки с выключенными FK, смешанные заказы, множественные `ACCEPTED` чеки) остановят миграцию на этапе `_preflight_migration_007()` с сохранением целостности.
4. **Отсутствие сервисной авторизации в Stage 3.1:** примитивы репозиториев (`OrderRepository`, `InventoryRepository.get_revealed_code`, `DeliveryRepository`) защищают инварианты данных на уровне user/order, но ролевая проверка `OPERATOR`/`ADMIN` и Telegram auth context относятся к слою сервисов/бота Stage 3.2.
5. **Сохранение имени `cost_rub_kopecks` (`OD-09`):** поле продолжает обозначать розничную цену без изменения Phase 1.

---

## 10. Намеренно НЕ реализовано из Stage 3.2

Следующие компоненты осознанно исключены из Stage 3.1 согласно мандату:
- **Migration `008`** (не требовалась и не создавалась).
- **Сервисный слой (`src/services/`)**: оркестрация checkout, TTL reaper, receipt review workflow, автоматическая финализация и отправка кодов покупателю.
- **Telegram-бот (`aiogram 3.x`)**: хендлеры команд/callback, FSM, клиентский/операторский интерфейс, blind operator resend UI.
- **FastAPI / Mini App / HTTP-вебхуки платёжных провайдеров / RBAC** и реальные сетевые клиенты.
- **Полноценные end-to-end пользовательские сценарии.**
