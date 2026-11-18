# AUDIT_README — Руководство для независимого аудита (Security Gate Phase 2)

Документ подготовлен исключительно по фактическому состоянию кода и истории Git для независимой проверки проекта через GitHub.

---

## 1. Назначение проекта

Сервис подбора оптимальных комбинаций цифровых ваучеров пополнения PlayStation Store (регионы `TR`/`US`, валюты `TRY`/`USD`) с расчётом в рублях (копейках), локальным хранением каталога, зашифрованного инвентаря кодов, заказов и платёжных чеков в SQLite.

Продуктовое описание и дорожная карта зафиксированы в [project.md](project.md), [project_context.md](project_context.md) и [AVITO_TRAFFIC_ENGINE.md](AVITO_TRAFFIC_ENGINE.md).

---

## 2. Архитектура и основные модули

Реализованы два базовых слоя (Phase 1 и Phase 2):

- **Phase 1 — Движок оптимального подбора (`src/engine/`, `src/calculator.py`):**
  - `src/engine/models.py` — контракты входных/выходных данных (`Region`, `Currency`, `Denomination`, `SelectedDenomination`, `OptimizationResult`, `ValidationError`, `validate_input_contract`), целочисленные ограничения (допустимый лимит на заказ: `DEFAULT_MAX_VOUCHERS_PER_ORDER = 6`, верхний предел `MAX_PERMITTED_VOUCHERS_PER_ORDER`).
  - `src/engine/knapsack.py` — `calculate_optimal_vouchers(...)` (Bounded Covering Knapsack без использования float в денежных вычислениях; оптимизация по минимальной стоимости в копейках, минимальному остатку на кошельке, минимальному количеству ваучеров и детерминированному tie-breaker).
  - `src/calculator.py` — публичный фасад экспорта над `src/engine`.
- **Phase 2 — Слой данных и безопасности (`src/db/`, `src/migrations/`):**
  - `src/db/connection.py` — `get_connection` / `create_connection`, `get_connection_context`, `transaction`, `backup_database`, `restore_database`.
  - `src/db/crypto.py` — `canonicalize_code`, `is_dev_or_test_environment`, `get_encryption_key`, `get_fingerprint_pepper`, `encrypt_payload`, `decrypt_payload`, `compute_fingerprint`, `compute_receipt_sha256`.
  - `src/db/migrations.py` — `ensure_migration_table`, `get_applied_migrations`, `discover_migration_files`, `run_migrations`.
  - `src/db/models.py` — перечисления статусов (`ProductType`, `ProductStatus`, `DeliveryMethod`, `ApprovalStatus`, `InventoryType`, `InventoryStatus`, `OrderStatus`, `ReceiptReviewStatus`, `UserStatus`), матрица допустимых переходов `ALLOWED_ORDER_TRANSITIONS`, доменные исключения и dataclass-модели.
  - `src/db/repositories.py` — транзакционные репозитории (`CatalogRepository`, `UserRepository`, `InventoryRepository`, `OrderRepository`, `ReceiptRepository`) и строгий типовой валидатор `_validate_int` (отклоняющий `bool`, `float`, `str`, `None` и недопустимые границы).
  - `src/migrations/001_initial_schema.sql`, `src/migrations/002_provider_scoped_receipts.sql` — SQL-миграции схемы.
  - `src/db/__init__.py` — реэкспорт публичного API пакета `src.db`.

---

## 3. Используемые технологии и зависимости

- **Язык:** Python 3.11+ (проверено на `Python 3.14.7`).
- **СУБД:** встроенный `sqlite3` (PRAGMA `foreign_keys = ON`, `journal_mode = WAL`, `synchronous = NORMAL`, `busy_timeout`).
- **Единственная внешняя зависимость (`requirements.txt`):** `cryptography>=41.0.0` (фактически установлено и протестировано `cryptography==50.0.2`).
- **Тестовый фреймворк:** стандартный `unittest`.
- **Инструменты будущих фаз (НЕ установлены и НЕ используются в коде):** `aiogram`, `FastAPI`, `Playwright` упоминаются только в проектной документации (`project.md`, `project_context.md`).

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

Ожидаемый фактический результат: `Ran 107 tests ... OK` (включая unit, конкурентные и security-remediation тесты в `tests/test_*.py`).

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
2. `ensure_migration_table()` создаёт таблицу `schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TIMESTAMP)`.
3. Для каждой неприменённой миграции `run_migrations()` формирует блок:
   ```sql
   BEGIN IMMEDIATE;
   <содержимое файла миграции>
   INSERT INTO schema_migrations (version, name, checksum) VALUES (<version>, '<escaped_name>', '<sha256>');
   COMMIT;
   ```
   и выполняет его через `conn.executescript(...)` без наивного разбиения по `;`, обеспечивая атомарный откат DDL/DML и записи версии при любой ошибке. При повторных запусках проверяются уникальность версий в `src/migrations/` (`discover_migration_files`) и SHA-256 контрольные суммы уже применённых миграций (для записей с заполненным `checksum`).

Применяемые миграции на текущий момент:
- `001_initial_schema.sql` — базовые таблицы (`schema_migrations`, `users`, `products`, `voucher_details`, `voucher_denominations`, `fixed_product_details`, `orders`, `order_items`, `inventory_items`, `payment_receipts`, `audit_events`) и индексы.
- `002_provider_scoped_receipts.sql` — добавление `provider_name` в `payment_receipts` и пересоздание уникального индекса `idx_receipts_provider_tx(provider_name, provider_tx_id)`.
- `003_inventory_and_integrity_guards.sql` — SQLite-триггеры уровня БД, обеспечивающие совместимость `ProductType <-> InventoryType`, принадлежность `denomination_id` товару `product_id` и совпадение `payment_receipts.user_id == orders.user_id`.

---

## 8. Где находится логика заказов, товаров, платежей и выдачи цифровых товаров

- **Товары и каталог:** `CatalogRepository` в `src/db/repositories.py` (таблицы `products`, `voucher_details`, `voucher_denominations`, `fixed_product_details`). Изменение `supply_method` у `FIXED_PRODUCT` сбрасывает `approval_status` и `products.status` в `DRAFT`.
- **Заказы и статусы:** `OrderRepository` в `src/db/repositories.py`, переходы состояний `ALLOWED_ORDER_TRANSITIONS` в `src/db/models.py` (таблицы `orders`, `order_items`, `audit_events`).
  - Все операции записи используют единую модель владения транзакциями `with transaction(self.conn, mode="IMMEDIATE"):` (`src/db/connection.py`), запрещающую выполнение поверх активной транзакции вызывающего кода.
  - `create_order()` проверяет статус пользователя, совпадение `product_type_snapshot`, статус `APPROVED` товара/поставщика, целостность суммы и валюты.
  - `transition_order_status()` проверяет допустимость перехода и использует оптимистичную блокировку (`WHERE id = ? AND status = ?`) с фиксацией в `audit_events`.
- **Платёжные чеки (ручная сверка, без автоподтверждения оплаты):** `ReceiptRepository` в `src/db/repositories.py` (таблица `payment_receipts`).
  - `submit_receipt()` внутри эксклюзивной `BEGIN IMMEDIATE` транзакции проверяет существование пользователя, принадлежность заказа (`orders.user_id == user_id`) без раскрытия чужих `order_id`, допустимый статус заказа (`PENDING_PAYMENT` или `PAYMENT_REVIEW`), размер файла (до 10 МБ), считает SHA-256 (`compute_receipt_sha256`), нормализует `provider_name` и `provider_tx_id`, отклоняет дубликаты по хешу файла и паре `(provider_name, provider_tx_id)`. Статус заказа автоматически в `PAID` **не** переводится.
- **Склад, матрица совместимости и резервирование цифровых товаров:** `InventoryRepository` в `src/db/repositories.py` и `ALLOWED_INVENTORY_TYPES_BY_PRODUCT_TYPE` в `src/db/models.py`.
  - Матрица совместимости: `VOUCHER -> {VOUCHER_CODE}`; `FIXED_PRODUCT -> {FIXED_CODE, ACTIVATION_DATA}`. Для `VOUCHER` обязательно задаётся `denomination_id`, принадлежащий `product_id` (`voucher_denominations.product_id == product_id`), что продублировано триггерами в миграции `003`.
  - Связь со статусами заказа (`src/db/models.py`):
    - `reserve_items_for_order()` разрешено только при `OrderStatus` в `RESERVABLE_ORDER_STATUSES` (`PENDING_PAYMENT`, `PAYMENT_REVIEW`, `PAID`, `FULFILLING`).
    - `release_order_reservations()` разрешено только при `OrderStatus` в `RELEASABLE_ORDER_STATUSES` (`PENDING_PAYMENT`, `PAYMENT_REVIEW`, `CANCELLED`, `REFUND_REVIEW`) и никогда не затрагивает `SOLD`-позиции и чужие заказы.
    - `finalize_order_sale()` разрешено только при `OrderStatus` в `FINALIZABLE_ORDER_STATUSES` (`PAID`, `FULFILLING`), запрещает повторное списание уже `SOLD`-товаров (`ConflictError`).
- **Контракт расшифровки кода (`InventoryRepository.get_revealed_code()`):**
  - Внешний/клиентский вызов расшифровки без предварительной аутентификации и авторизации (RBAC / проверка Telegram-сессии владельца или администратора) **строго запрещён**.
  - Метод является внутренним примитивом репозитория: безусловно блокирует `QUARANTINED` и `DISABLED` позиции; при передаче контекста заказа (`expected_order_id`, `expected_user_id`) единым `JOIN`-запросом проверяет привязку `item -> order`, принадлежность заказа пользователю (`orders.user_id == expected_user_id`), обязательный статус позиции `SOLD` (после `finalize_order_sale()`) и статус заказа в `REVEALABLE_ORDER_STATUSES` (`PAID`, `FULFILLING`, `COMPLETED`).
  - **Рекомендация для Phase 3:** реализовать сервисный слой `FulfillmentService` / Telegram-хендлер с обязательной проверкой аутентификации пользователя (`external_id` -> `user.id`), статуса заказа и ролей оператора/админа перед вызовом `get_revealed_code(..., expected_order_id=..., expected_user_id=...)`.
  - Пользовательский UI/бот-выдача клиенту в Phase 2 — **не реализована**.

---

## 9. Где реализованы криптография, хранение секретов и управление ключами

Вся криптографическая логика находится в `src/db/crypto.py` (используется `cryptography.fernet.Fernet`, `hashlib`, `hmac`):

- **Шифрование кодов инвентаря:** `encrypt_payload()` / `decrypt_payload()` используют `Fernet` (AES-128-CBC + HMAC-SHA256) с предварительной каноникализацией кода (`canonicalize_code`, обрезка граничных пробелов/переводов строк).
- **Дедупликация инвентаря:** `compute_fingerprint()` вычисляет HMAC-SHA256 с секретным ключом `FINGERPRINT_PEPPER` (или `INVENTORY_FINGERPRINT_PEPPER`). Поддержаны `migrate_legacy_fingerprints()` и `rotate_fingerprint_pepper()` в `InventoryRepository`.
- **Дедупликация чеков:** `compute_receipt_sha256()` (SHA-256 по сырым байтам файла).
- **Хранение и использование `INVENTORY_ENCRYPTION_KEY`:**
  - Вне dev/test сред ключ обязан передаваться через переменную окружения `INVENTORY_ENCRYPTION_KEY` (32 url-safe base64 байта Fernet; валидируется при обращении через `Fernet(key_bytes)`). Ключ никогда не сохраняется в SQLite и не попадает в сообщения об ошибках.
  - `is_dev_or_test_environment()` разрешает fallback (`_DEV_FALLBACK_KEY`, `_DEV_FALLBACK_PEPPER`) **только** если задана хотя бы одна из `ENV`/`APP_ENV` и **все** заданные значения входят в `{"test", "testing", "dev", "development"}`. При `production`, `staging`, неизвестных значениях, конфликте или отсутствии обеих переменных fallback запрещён (`CryptoConfigurationError`).
- **Последствия потери `INVENTORY_ENCRYPTION_KEY`:**
  - Полная и необратимая потеря возможности расшифровать все зашифрованные записи `inventory_items.encrypted_payload` (включая невозможность выдачи купленных и свободных кодов, а также невозможность выполнить `rotate_fingerprint_pepper()`, так как пересчёт HMAC требует предварительной расшифровки payload).
- **Процедура резервирования и восстановления `INVENTORY_ENCRYPTION_KEY`:**
  - Ключ должен храниться и резервироваться **отдельно** от дампов SQLite (`backup_database`) — в защищённом менеджере секретов и/или зашифрованной оффлайн-копии (cold backup). Резервная копия БД без соответствующего `INVENTORY_ENCRYPTION_KEY` непригодна для выдачи товаров. При восстановлении сервиса из бэкапа сперва восстанавливается целевой ключ в окружении, затем БД (`restore_database`), и выполняется контрольная проверка расшифровки тестовой/канареечной записи.
- **План безопасной ротации `INVENTORY_ENCRYPTION_KEY` (в коде Phase 2 пока НЕ реализовано — требует отдельной задачи):**
  1. Остановка всех писателей/воркеров (quiesce) и обязательный бэкап БД + текущего ключа `old_key`.
  2. Транзакционное перешифрование всех `inventory_items.encrypted_payload` в `BEGIN IMMEDIATE` (расшифровка `old_key` -> шифрование `new_key`, с полным rollback при ошибке) либо двухключевой переход через `cryptography.fernet.MultiFernet([new_key, old_key])`.
  3. Канареечная проверка расшифровки `new_key` на контрольной записи.
  4. Выкатка нового `INVENTORY_ENCRYPTION_KEY` во все инстансы, удаление `old_key` из активного чтения после завершения переходного окна, возобновление трафика.
  - **Риски:** сбой между коммитом перешифрованной БД и выкаткой `new_key` делает инстансы со старым ключом неспособными расшифровывать коды; частичное неатомарное обновление ломает расшифровку.
- **Отличие ротации `INVENTORY_ENCRYPTION_KEY` от ротации `FINGERPRINT_PEPPER`:**
  - Ротация `INVENTORY_ENCRYPTION_KEY` меняет конфиденциальные шифротексты `encrypted_payload`, требует наличия и `old_key`, и `new_key` и при потере ключа ведёт к необратимой потере данных.
  - Ротация `FINGERPRINT_PEPPER` (`InventoryRepository.rotate_fingerprint_pepper()` уже реализована) оставляет `encrypted_payload` без изменений, использует инвариантный `INVENTORY_ENCRYPTION_KEY` для чтения кода и пересчитывает только односторонние HMAC-SHA256 отпечатки `inventory_items.fingerprint` (при ошибке рассогласуется только дедупликация новых загрузок до выравнивания переменной окружения, данные не теряются).

---

## 10. Какие функции пока не реализованы или отключены

- Telegram-бот (`aiogram 3.x`) и обработчики команд/состояний FSM — **не реализованы**.
- Telegram Mini App (Frontend) и Backend API (FastAPI) — **не реализованы**.
- Автоматический приём реальных платежей (СБП / CryptoBot / вебхуки платёжных провайдеров) — **не реализовано**.
- Автоматическая выдача цифровых товаров конечным пользователям (бот/веб-интерфейс) — **не реализовано**.
- Парсер PS Store и каталог PS Plus Extra / Deluxe — **не реализованы**.
- AI-консультант и AI-маркетолог — **не реализованы**.
- Автоферма аккаунтов (Playwright) — **не реализована**.
- HTTP-прослойки, аутентификация админки/RBAC и внешние веб-запросы — **не реализованы**.

---

## 11. Какие ограничения известны

1. **SQLite single-writer:** архитектурно SQLite допускает одного активного писателя; при высокой конкурентности записи запросы сериализуются в пределах `timeout_seconds` (`busy_timeout`).
2. **Операционный регламент `restore_database()` (`src/db/connection.py`):** перед восстановлением БД из резервной копии все процессы приложения и воркеры обязаны быть остановлены, а все соединения закрыты. Пробы `BEGIN EXCLUSIVE` и `PRAGMA wal_checkpoint(TRUNCATE)` являются защитой в глубину, но SQLite не гарантирует межпроцессное обнаружение всех idle-соединений.
3. **Операционный регламент `rotate_fingerprint_pepper()` (`src/db/repositories.py`):** требует остановки параллельных импортов (quiesce) до завершения ротации и обновления переменной окружения во всех инстансах.
4. **Трассировка в конкурентном тесте (`tests/test_orders.py`):** сценарий 1 в `test_real_concurrency_approval_revocation_during_create_order` использует `sqlite3.Connection.set_trace_callback` для детерминированного воспроизведения удержания транзакции во время попытки отзыва.
5. **Отсутствие интеграционного слоя:** в репозитории нет исполняемого entrypoint (`main.py` / CLI / сервера), логика тестируется напрямую через модули `src.engine`, `src.calculator` и `src.db`.
6. **Непроверенные внешние интеграции:** работоспособность Telegram Bot API, внешних бирж ваучеров и платёжных шлюзов — **не проверено** (отсутствуют в кодовой базе).

---

## 12. Какие действия запрещены до завершения аудита

До получения явного положительного вердикта независимого аудита (Security Gate Phase 2) запрещено:

- Начинать Phase 3 (разработку Telegram-бота, FastAPI и Mini App).
- Подключать реальные платёжные системы и обработку живых платежей.
- Включать автоматическую выдачу реальных цифровых кодов/аккаунтов.
- Выполнять деплой на сервер или в production-окружение.
- Помещать реальные ключи шифрования, токены или пользовательские базы данных в репозиторий.

---

## 13. История изменений (Security Gate Phase 2 — заключительный этап)

История Git сохранена без переписывания (`rebase`/`squash` не применялись):

- **Базовый commit (`f849bf1ede96c793ae6baa4b1b1605dd7688c6bb`):**
  `fix(security-gate): remediate env fallback checks, transaction ownership, deterministic concurrency, checkpoint validation, and strict typing`
  - Изменённые файлы (8): `src/db/connection.py`, `src/db/crypto.py`, `src/db/repositories.py`, `tests/test_crypto_remediation.py`, `tests/test_inventory.py`, `tests/test_orders.py`, `tests/test_receipts_and_catalog_remediation.py`, `tests/test_restore_safety.py`.
  - Исправления замечаний аудита: строгая проверка `ENV`/`APP_ENV` (блокировка смешанных, `staging`, `unknown` и `unset` сред), проверка `in_transaction` в `OrderRepository.transition_order_status`, строгий целочисленный валидатор `_validate_int` (отсечение `bool`) во всех репозиториях, проверка `busy == 0` и `log_frames == checkpointed_frames` в `PRAGMA wal_checkpoint(TRUNCATE)` и двухфазное сохранение `.orig_bak_*` в `restore_database()`, детерминированный тест конкурентности заказа и отзыва.
- **Промежуточный commit (`507e522603e6f543c7db2f78bac03222111ff063`):**
  `test(orders): make real concurrency test hold active transaction during revocation attempt`
  - Изменённые файлы (1): `tests/test_orders.py`.
  - Исправление замечаний аудита: сценарий 1 в `test_real_concurrency_approval_revocation_during_create_order` начал реально удерживать транзакцию `create_order()` через `set_trace_callback` до коммита, а второй поток проверял блокировку пробным соединением, завершение потоков через `assertFalse(t.is_alive())`.
- **Текущий commit (`c729cd6094384c4029017419d4aadfc7527f7726`):**
  `test(orders): start real SQL UPDATE revocation while create_order holds transaction`
  - Изменённые файлы (1): `tests/test_orders.py`.
  - Исправление замечаний аудита: сценарий 1 перестроен так, чтобы вместо синтетической lock-пробы **настоящий SQL `UPDATE fixed_product_details`** стартовал и упирался в write lock SQLite, пока `create_order()` ещё удерживает транзакцию (до `allow_commit.set()`), с проверкой инвариантов `update_started_before_commit`, `update_completed_after_commit`, `1 order / 1 order_item`, `approval_status == 'DRAFT'`, `0` осиротевших записей и чистого завершения потоков.
