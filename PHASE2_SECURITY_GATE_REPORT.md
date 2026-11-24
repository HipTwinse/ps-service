# PHASE 2 SECURITY GATE REPORT

## A. Audit Metadata

- **Дата и время:** 2026-11-19T09:25:17+10:00
- **Репозиторий:** `HipTwinse/ps-service`
- **Проверенная ветка:** `security/phase2-remediation`
- **Точный HEAD SHA:** `a7a8709d16bd8e28f9b6f49b2f0177bd3b91fef7` (с учётом незакоммиченных локальных изменений в рабочем дереве)
- **Base SHA (`main` и `origin/main`):** `b7b0e05b36c283b07ba0fedd6bfbbc3be49a0eba`
- **Версии runtime (локально):**
  - `Python 3.14.7` (`[MSC v.1944 64 bit (AMD64)]`)
  - `SQLite 3.50.4`
- **Использованный аудитор:** Агент `Argo` в текущей среде недоступен. Независимая проверка выполнена силами Antigravity с привлечением изолированного read-only субагента `research` (не является аудитом отдельного внешнего агента `Argo`).
- **Подтверждение независимости проверки:** Production-код, миграции и тесты не модифицировались, найденные дефекты не исправлялись, Git-операции (`commit`, `push`, `PR`, `merge`, `deploy`) не выполнялись. Вердикт сформирован исключительно по результатам прямого анализа кода, воспроизведения сценариев и прогона тестов.

---

## B. Scope

### Проверенные компоненты и файлы
- **Управление соединениями и транзакциями:** `src/db/connection.py`
- **Миграции и схема БД:** `src/db/migrations.py`, `src/migrations/001_initial_schema.sql`, `src/migrations/002_provider_scoped_receipts.sql`, `src/migrations/003_inventory_and_integrity_guards.sql`
- **Модели и криптография:** `src/db/models.py`, `src/db/crypto.py`
- **Репозитории:** `src/db/repositories.py` (`CatalogRepository`, `UserRepository`, `InventoryRepository`, `OrderRepository`, `ReceiptRepository`)
- **Набор тестов:** `tests/test_crypto.py`, `tests/test_crypto_remediation.py`, `tests/test_inventory.py`, `tests/test_orders.py`, `tests/test_phase2_audit_remediation.py`, `tests/test_phase2_remediation_followup.py`, `tests/test_restore_safety.py`

### Что не удалось проверить
- Совместимость с **Python 3.11** и **Python 3.12** (интерпретаторы локально отсутствуют).
- Прогон **GitHub Actions CI**.
- Компоненты **Phase 3** (Telegram-бот, FastAPI, Mini App, платежи — этап заблокирован).

---

## C. Findings

### CRITICAL

#### `SG-P2-001` — Отсутствие инварианта `denomination_id` в `order_items`, `reserve_items_for_order()`, `finalize_order_sale()` и триггерах БД (Cross-Denomination Fulfillment)
- **Severity:** `CRITICAL`
- **Файл и строки:**
  - `src/migrations/001_initial_schema.sql:88-98` (`order_items`)
  - `src/db/models.py:245-254` (`OrderItem`)
  - `src/db/repositories.py:943-1054` (`OrderRepository.create_order`)
  - `src/db/repositories.py:670-712` (`InventoryRepository.reserve_items_for_order`)
  - `src/db/repositories.py:807-841` (`InventoryRepository.finalize_order_sale`)
  - `src/migrations/003_inventory_and_integrity_guards.sql:65-113`
- **Описание дефекта:** Таблица `order_items` не имеет столбца `denomination_id` (содержит только невалидируемый `metadata_json`). `create_order()` не сохраняет и не проверяет номинал для `VOUCHER`-позиций. `reserve_items_for_order()` проверяет принадлежность переданного `denomination_id` продукту `product_id`, но лимиты заказа (`ordered_qty` и `allocated_qty`) считает только по `(order_id, product_id)`. `finalize_order_sale()` также агрегирует и сверяет `order_items` и `inventory_items` только через `GROUP BY product_id`. Триггеры миграции `003` проверяют лишь `inventory_items.denomination_id -> inventory_items.product_id`, но не связь с `order_items`.
- **Предпосылки эксплуатации:** Наличие одного `VOUCHER`-продукта с несколькими номиналами в `voucher_denominations` (поддерживается схемой `001`).
- **Сценарий воспроизведения (воспроизведено in-memory в ходе аудита):**
  1. Создан `VOUCHER`-продукт `PSN TR` (`prod.id = 1`) с двумя номиналами: `d_100` (`100 TRY`, `30000` коп.) и `d_1000` (`1000 TRY`, `300000` коп.), загружены коды `CODE-100-TRY` и `CODE-1000-TRY`.
  2. Создан заказ на `1` единицу `prod.id = 1` с `unit_price_minor = 30000` (`total_amount_minor = 30000`).
  3. Вызван `reserve_items_for_order(order.id, prod.id, 1, d_1000.id)`, заказ переведён в `PAID`, вызван `finalize_order_sale(order.id)` (`sold = 1`), затем `get_revealed_code(..., expected_order_id=order.id, expected_user_id=user.id)` вернул `'CODE-1000-TRY'`.
- **Влияние:** Прямой финансовый ущерб и нарушение целостности выдачи: покупатель, оплативший дешёвый номинал (или заказ с несколькими номиналами одного `product_id`), может получить инвентарь более дорогого номинала.
- **Рекомендация:** Добавить `denomination_id` в `order_items` (миграция `004` с `REFERENCES voucher_denominations(id) ON DELETE RESTRICT`, pre-flight и триггерами соответствия `product_type`/`product_id`), сохранять `denomination_id` в `create_order()`, вести учёт и сверку в `reserve_items_for_order()` и `finalize_order_sale()` по паре `(product_id, denomination_id)`.
- **Статус:** `confirmed`

---

### HIGH

#### `SG-P2-002` — `ON DELETE SET NULL` на `inventory_items.reserved_order_id` приводит к осиротению `RESERVED`/`SOLD` инвентаря
- **Severity:** `HIGH`
- **Файл и строки:**
  - `src/migrations/001_initial_schema.sql:109-112`
  - `src/migrations/003_inventory_and_integrity_guards.sql:90-113`
- **Описание дефекта:** Внешний ключ `inventory_items.reserved_order_id` объявлен с `ON DELETE SET NULL`, при этом в БД отсутствует `CHECK` или триггер, связывающий `status` с `reserved_order_id` (`trg_inventory_items_validate_update` срабатывает только на `UPDATE OF product_id, denomination_id, inventory_type`).
- **Предпосылки эксплуатации:** Удаление записи из `orders` (при отсутствии `payment_receipts`, у которых стоит `ON DELETE RESTRICT`).
- **Сценарий воспроизведения:** Зарезервировать или финализировать инвентарь за заказом (`status = 'RESERVED'` или `'SOLD'`, `reserved_order_id = order.id`). Выполнить `DELETE FROM orders WHERE id = ?`. Строки `order_items` удаляются каскадно, а у `inventory_items` выставляется `reserved_order_id = NULL` при сохранении `status = 'RESERVED'`/`'SOLD'`.
- **Влияние:** Инвентарь навсегда застревает в `RESERVED`/`SOLD` без привязки к заказу; `release_order_reservations()` не может его освободить, а `get_revealed_code()` отклоняет выдачу.
- **Рекомендация:** Заменить политику удаления на `ON DELETE RESTRICT` для `reserved_order_id` (через пересоздание таблицы в миграции) и добавить DB-инвариант/триггер: `AVAILABLE`/`QUARANTINED`/`DISABLED` не могут иметь `reserved_order_id` (или по политике), а `RESERVED`/`SOLD` обязаны иметь `reserved_order_id IS NOT NULL`.
- **Статус:** `confirmed`

#### `SG-P2-003` — Отсутствие валидации цены `unit_price_minor` относительно каталога в `OrderRepository.create_order()`
- **Severity:** `HIGH`
- **Файл и строки:** `src/db/repositories.py:970-1052`
- **Описание дефекта:** `create_order()` проверяет только внутреннюю арифметику `total_amount_minor == sum(quantity * unit_price_minor)`, но принимает произвольное `unit_price_minor >= 0` от вызывающей стороны, не сверяя цену с `fixed_product_details.price_rub_kopecks` или `voucher_denominations.cost_rub_kopecks` (и не требуя `denomination_id` для `VOUCHER`).
- **Предпосылки эксплуатации:** Вызов `OrderRepository.create_order()` с заниженной ценой позиции.
- **Сценарий воспроизведения:** Передать в `create_order()` `product_id = fixed_prod.id` (каталожная цена `450000` коп.) с `unit_price_minor = 1` и `total_amount_minor = 1`. Заказ успешно создаётся.
- **Влияние:** Риск создания заказов по произвольно заниженной или нулевой цене при любой ошибке/подмене на уровне сервисного/API-слоя (Phase 3).
- **Рекомендация:** Валидировать цену позиции по каталогу на момент создания заказа внутри `BEGIN IMMEDIATE` в `create_order()` (с учётом выбранного `denomination_id` для `VOUCHER` и `fixed_product_details` для `FIXED_PRODUCT`).
- **Статус:** `confirmed`

---

### MEDIUM

#### `SG-P2-004` — Рассинхронизация жизненного цикла заказа/инвентаря и невозможность кросс-репозиторной транзакции
- **Severity:** `MEDIUM`
- **Файл и строки:**
  - `src/db/connection.py:97-100`
  - `src/db/models.py:69-77, 92-101`
  - `src/db/repositories.py:1056-1111` (`transition_order_status`)
- **Описание дефекта:**
  - `transition_order_status()` разрешает переход `FULFILLING -> COMPLETED` без проверки того, что весь зарезервированный инвентарь переведён в `SOLD`. После `COMPLETED` ни `finalize_order_sale()`, ни `release_order_reservations()` не могут обработать оставшиеся `RESERVED` позиции.
  - Переход в `CANCELLED` не освобождает автоматически `RESERVED` инвентарь.
  - `transaction()` отклоняет вложенный вызов при `conn.in_transaction == True` без поддержки `SAVEPOINT`, а каждый метод репозитория открывает собственную транзакцию, поэтому вызывающий код не может атомарно объединить `transition_order_status()` и `finalize_order_sale()`/`release_order_reservations()`.
- **Предпосылки эксплуатации:** Сбой между последовательными вызовами репозиториев или некорректный порядок переходов статуса заказа.
- **Сценарий воспроизведения:** Для заказа с `RESERVED` инвентарём выполнить `PENDING_PAYMENT -> PAYMENT_REVIEW -> PAID -> FULFILLING -> COMPLETED` без вызова `finalize_order_sale()`. Последующий вызов `finalize_order_sale(order.id)` падает с `InvalidStateTransitionError`.
- **Влияние:** Зависание инвентаря в `RESERVED` при завершённом/отменённом заказе или неатомарность многошаговых бизнес-операций.
- **Рекомендация:** Запретить `FULFILLING -> COMPLETED` при наличии нефинализированных/неполных резервов и поддержать композицию транзакций (или `SAVEPOINT` / unit-of-work) между репозиториями.
- **Статус:** `confirmed`

#### `SG-P2-005` — Граничные случаи парсера `_split_sql_statements()` в `migrations.py`
- **Severity:** `MEDIUM`
- **Файл и строки:** `src/db/migrations.py:88-110`
- **Описание дефекта:** Построчное накопление с проверкой `sqlite3.complete_statement(buffer)` возвращает `True`, если на одной строке находятся два оператора через `;` (например, `CREATE TABLE a(x INT); CREATE TABLE b(y INT);`), после чего `conn.execute(stmt)` падает с `sqlite3.ProgrammingError: You can only execute one statement at a time`. Кроме того, завершающий блочный комментарий `/* ... */` после последнего `;` остаётся в `buffer`, где `leftover_lines` игнорирует только `--`, и вызывает ложный `ValueError: Incomplete SQL statement`.
- **Предпосылки эксплуатации:** Будущая миграция, содержащая два SQL-оператора в одной строке или завершающий блочный комментарий `/* ... */`.
- **Сценарий воспроизведения:** Передать `"CREATE TABLE a(x INT); CREATE TABLE b(y INT);"` или `"CREATE TABLE a(x INT);\n/* trailing */"` в `_split_sql_statements()`.
- **Влияние:** Отказ применения валидных будущих SQL-миграций. Текущие миграции `001`–`003` под эти паттерны не попадают.
- **Рекомендация:** Использовать полноценный SQL-токенизатор или корректно обрабатывать блочные комментарии и посимвольный поиск границ `complete_statement`.
- **Статус:** `confirmed`

#### `SG-P2-006` — Пробелы в прямых SQL-ограничениях миграции `003`
- **Severity:** `MEDIUM`
- **Файл и строки:** `src/migrations/003_inventory_and_integrity_guards.sql:4-132`
- **Описание дефекта:**
  - Отсутствует pre-flight и триггер `BEFORE INSERT`/`UPDATE` на `voucher_denominations`, запрещающий привязку номинала к `FIXED_PRODUCT`.
  - `trg_inventory_items_validate_update` срабатывает только на `UPDATE OF product_id, denomination_id, inventory_type`, оставляя прямые SQL-обновления `status` и `reserved_order_id` без защиты (возможен прямой откат `SOLD -> AVAILABLE` или `RESERVED` с `reserved_order_id = NULL`).
  - Pre-flight запросы используют `INNER JOIN products`, поэтому при legacy-строках с битым `product_id` (вставленных при `foreign_keys = OFF`) проверка их пропустит.
- **Предпосылки эксплуатации:** Прямой SQL-доступ или ошибка миграции legacy-данных.
- **Сценарий воспроизведения:** `UPDATE inventory_items SET status = 'AVAILABLE', reserved_order_id = NULL WHERE status = 'SOLD';` выполняется без блокировки триггером.
- **Влияние:** Снижение гарантий защиты на уровне БД (defense-in-depth).
- **Рекомендация:** Расширить триггеры миграции `003` (или добавить `004`) на `voucher_denominations`, переходы `inventory_items.status`/`reserved_order_id` и использовать `LEFT JOIN` в pre-flight проверках.
- **Статус:** `confirmed`

#### `SG-P2-007` — Недостатки качества конкурентных тестов (`time.sleep`, отсутствие `is_alive()`, отсутствие race-теста резервирования)
- **Severity:** `MEDIUM`
- **Файл и строки:**
  - `tests/test_phase2_remediation_followup.py:684-712` (`test_concurrent_run_migrations_serializes_safely`)
  - `tests/test_phase2_remediation_followup.py:744-792` (`test_concurrent_run_migrations_waits_on_immediate_lock_and_skips_applied`)
  - `tests/test_orders.py:808-813` (`test_real_concurrency_approval_revocation_during_create_order`)
  - `tests/test_inventory.py:1, 31`
- **Описание дефекта:**
  - В `test_concurrent_run_migrations_waits_on_immediate_lock_and_skips_applied` событие `started.set()` выставляется до вызова `run_migrations()`, затем используется произвольный `time.sleep(0.15)` и не проверяется `not t2.is_alive()` после `t2.join(timeout=10.0)`.
  - В `test_concurrent_run_migrations_serializes_safely` не проверяются `not t1.is_alive()` и `not t2.is_alive()` после `join(timeout=10.0)`.
  - В `test_orders.py:809` используется `time.sleep(0.05)` после `set_trace_callback` (хотя `is_alive()` проверяется до и после `join`).
  - В `test_inventory.py` заявлено тестирование конкурентной безопасности, но отсутствует многопоточный race-тест `reserve_items_for_order()` за последнюю доступную единицу товара.
- **Предпосылки эксплуатации:** Высокая нагрузка на CI / медленный I/O.
- **Сценарий воспроизведения:** При задержке старта потока `runner2` > `0.15s` в `test_concurrent_run_migrations_waits_on_immediate_lock_and_skips_applied` `conn1.commit()` произойдёт до входа `runner2` в `BEGIN IMMEDIATE`, и тест проверит последовательный, а не конкурентный путь.
- **Влияние:** Ложноположительная уверенность в детерминизме конкурентных тестов и отсутствие прямого многопоточного race-теста резервирования инвентаря.
- **Рекомендация:** Заменить `time.sleep()` на детерминированную синхронизацию (например, `set_trace_callback` / `Event` на фактический вход в SQLite), добавить `assertFalse(t.is_alive())` во все поточные тесты и добавить многопоточный тест гонки `reserve_items_for_order()`.
- **Статус:** `confirmed`

---

### LOW

#### `SG-P2-008` — Маскировка ошибок `IntegrityError` и отсутствие проверки `region`/`currency` в `add_voucher_denomination()`
- **Severity:** `LOW`
- **Файл и строки:** `src/db/repositories.py:112-154`
- **Описание дефекта:** `add_voucher_denomination()` не сверяет `region`/`currency` с `voucher_details` продукта и в `except sqlite3.IntegrityError` превращает любую ошибку целостности (включая `CHECK` на `region`/`currency`) в `ConflictError("...already exists")`.
- **Статус:** `confirmed`

#### `SG-P2-009` — Неполная валидация типа/значения `currency` в `OrderRepository.create_order()`
- **Severity:** `LOW`
- **Файл и строки:** `src/db/repositories.py:973-978, 1035-1041`
- **Описание дефекта:** Нестроковый `item["currency"]` вызывает `AttributeError` вместо `ValidationError`; неподдерживаемая валюта заказа (например, `'EUR'`) доходит до `CHECK`-ограничения SQLite и падает с `sqlite3.IntegrityError`.
- **Статус:** `confirmed`

#### `SG-P2-010` — Тихий no-op при несуществующих ID в `update_fixed_product_approval()` и `update_user_status()`
- **Severity:** `LOW`
- **Файл и строки:** `src/db/repositories.py:278-296, 398-404`
- **Описание дефекта:** Отсутствует проверка `cur.rowcount == 0`, из-за чего обновление несуществующего `product_id`/`user_id` (или `VOUCHER` в `update_fixed_product_approval()`) завершается без `NotFoundError`.
- **Статус:** `confirmed`

#### `SG-P2-011` — Неиспользуемый параметр `old_pepper` в `rotate_fingerprint_pepper()`
- **Severity:** `LOW`
- **Файл и строки:** `src/db/repositories.py:549-612`
- **Описание дефекта:** Параметр `old_pepper: Optional[bytes] = None` принимается сигнатурой, но не используется для верификации старых fingerprint перед пересчётом.
- **Статус:** `confirmed`

#### `SG-P2-012` — Fallback пустого ключа в `crypto.py` и отсутствие минимальной длины `FINGERPRINT_PEPPER`
- **Severity:** `LOW`
- **Файл и строки:** `src/db/crypto.py:111-121, 138, 152`
- **Описание дефекта:** `Fernet(key or get_encryption_key())` молча подставляет системный ключ при передаче пустого `b""`; `get_fingerprint_pepper()` принимает любую непустую строку (вплоть до 1 байта).
- **Статус:** `confirmed`

#### `SG-P2-013` — `get_revealed_code()` не проверяет `users.status` и различает тексты ошибок
- **Severity:** `LOW`
- **Файл и строки:** `src/db/repositories.py:859-935`
- **Описание дефекта:** Заблокированный пользователь (`BLOCKED`) может раскрыть код ранее оплаченного заказа; различные тексты `ValidationError` позволяют различать статус и принадлежность `item_id` при прямом вызове репозитория.
- **Статус:** `confirmed`

#### `SG-P2-014` — `restore_database()` перехватывает `Exception` вместо `BaseException` и чтение DTO после `commit`
- **Severity:** `LOW`
- **Файл и строки:** `src/db/connection.py:232, 295`; `src/db/repositories.py:110, 150, 199, 1054, 1111, 1246, 1271`
- **Описание дефекта:** При `BaseException` во время подмены файлов в `restore_database()` автоматический откат пропускается (backup-копии `orig_bak_*` остаются на диске). Методы записи репозиториев читают возвращаемый DTO уже после выхода из `with transaction(...)`.
- **Статус:** `confirmed`

---

### INFO

#### `SG-P2-015` — Потеря метаданных перехода в `audit_events` при пустом `notes`
- **Severity:** `INFO`
- **Файл и строки:** `src/db/repositories.py:1101`
- **Описание дефекта:** `transition_order_status()` записывает `details_json = None`, если `notes` не передан, теряя `from_status` и `to_status` в таблице аудита.
- **Статус:** `confirmed`

#### `SG-P2-016` — Незначительные расхождения валидации строковых полей
- **Severity:** `INFO`
- **Файл и строки:** `src/db/repositories.py:156-199, 1184`
- **Описание дефекта:** `create_fixed_product()` не отклоняет пустой/пробельный `display_name` и `supply_method`; `submit_receipt()` вызывает `AttributeError` при нестроковом `provider_name`.
- **Статус:** `confirmed`

---

## D. Test Results

- **Команда запуска:** `python -m unittest discover -v tests`
- **Результат:** `130 passed, 0 failed, 0 skipped` (`9.241s`)
- **Ошибки и предупреждения:** `0` ошибок; `git diff --check` завершился с кодом `0` (стандартные предупреждения Windows LF/CRLF).
- **Проверенные конкурентные сценарии:**
  - `tests/test_orders.py`: `SC1` и `SC2` (отзыв одобрения продукта vs `create_order`), используют `set_trace_callback` и проверяют `is_alive()`, но `SC1` содержит `time.sleep(0.05)`.
  - `tests/test_phase2_audit_remediation.py`: конкурентный `reserve` и конкурентный `finalize_order_sale` vs `release_order_reservations`, используют `Barrier(2)` и проверяют `not t.is_alive()`.
  - `tests/test_phase2_remediation_followup.py`: конкурентный `run_migrations` и ожидание `BEGIN IMMEDIATE` блокировки — без `is_alive()` после `join()`, а второй тест использует `time.sleep(0.15)`.
  - Многопоточный race-тест для `reserve_items_for_order()` за последнюю единицу товара в `test_inventory.py` отсутствует.
- **Непроверенные версии runtime:** `Python 3.11`, `Python 3.12`.
- **Результат denomination consistency tests:** Тестов, проверяющих совпадение `denomination_id` между `order_items` и `inventory_items`, в наборе **нет** (ввиду отсутствия столбца `denomination_id` в `order_items`). Воспроизведённый сценарий подмены номинала (`100 TRY` в заказе -> выдача `1000 TRY`) полностью проходит без исключений.

---

## E. Known Risks and Limitations

1. **Бизнес-инвариант `denomination_id` не обеспечен** ни на уровне схемы БД, ни на уровне Python-репозиториев (`SG-P2-001`).
2. **Непроверенные целевые версии Python (`3.11` / `3.12`):** результаты получены только на `Python 3.14.7` / `SQLite 3.50.4`.
3. **Недоступность агента `Argo`:** проверка выполнена силами Antigravity и встроенного read-only субагента `research`, а не отдельным внешним аудитором `Argo`.
4. **Незакоммиченные изменения в рабочем дереве:** текущие правки на ветке `security/phase2-remediation` не зафиксированы коммитом и не прогнаны через GitHub Actions CI.

---

## F. Final Verdict

**FAIL**

- **Обоснование:** Обнаружен `CRITICAL` дефект (`SG-P2-001` — отсутствие `denomination_id` в `order_items` и сверки номинала при резервировании/финализации заказа, позволяющее выдать более дорогой номинал при оплате дешёвого) и `HIGH` дефекты (`SG-P2-002` — `ON DELETE SET NULL` на `reserved_order_id`, осиротение `RESERVED`/`SOLD` инвентаря; `SG-P2-003` — отсутствие проверки каталожной цены в `create_order()`).
- **Допуск к следующему этапу (Phase 3):** **НЕТ (строго заблокировано)** до устранения `CRITICAL`/`HIGH` замечаний и прохождения повторного независимого Security Gate (включая CI на Python `3.11`/`3.12`).
