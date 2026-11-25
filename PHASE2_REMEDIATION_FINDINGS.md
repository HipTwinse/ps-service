# PHASE 2 REMEDIATION FINDINGS (`SG-P2-001`, `SG-P2-002`, `SG-P2-003`)

- **Дата:** 2026-11-24
- **Ветка:** `security/phase2-remediation`
- **Baseline HEAD SHA:** `a7a8709d16bd8e28f9b6f49b2f0177bd3b91fef7` (с предсуществующими локальными изменениями)
- **Base `main` / `origin/main` SHA:** `b7b0e05b36c283b07ba0fedd6bfbbc3be49a0eba`
- **Runtime (локально):** `Python 3.14.7`, `SQLite 3.50.4` (`Python 3.11` и `3.12` локально недоступны и не проверялись)
- **Phase 1 (`src/engine/`, `src/calculator.py`):** не изменялась (`0` diff относительно `main`)

---

## 1. `SG-P2-001` (CRITICAL) — Подмена `denomination_id` (Cross-Denomination Fulfillment)

- **Исходная проблема:**
  Заказ на номинал `100 TRY` (`30000` коп.) мог зарезервировать, финализировать и раскрыть код от номинала `1000 TRY` (`CODE-1000-TRY`) того же `VOUCHER`-продукта.
- **Причина:**
  1. Таблица `order_items` (`001_initial_schema.sql`) не содержала столбца `denomination_id`.
  2. `OrderRepository.create_order()` не требовал и не сохранял `denomination_id`.
  3. `InventoryRepository.reserve_items_for_order()` и `finalize_order_sale()` сверяли количества только по `product_id`, без учёта `denomination_id`.
  4. На уровне БД отсутствовали ограничения/триггеры, связывающие `order_items.denomination_id` с `inventory_items.denomination_id`.
- **Исправленные файлы и точные строки:**
  - `src/migrations/004_order_item_denomination.sql:1-93`
  - `src/migrations/005_inventory_order_delete_restrict.sql:22-38, 141-151, 178-188`
  - `src/db/models.py:244-255` (`OrderItem.denomination_id`)
  - `src/db/repositories.py:670-738` (`InventoryRepository.reserve_items_for_order`)
  - `src/db/repositories.py:799-883` (`InventoryRepository.finalize_order_sale`)
  - `src/db/repositories.py:971-1143` (`OrderRepository.create_order`)
  - `src/db/repositories.py:1216-1234` (`OrderRepository.get_order_items`)
- **Новое поведение:**
  - `order_items.denomination_id` добавлен как `INTEGER REFERENCES voucher_denominations(id) ON DELETE RESTRICT` с индексом `idx_order_items_order_product_denom`.
  - Для `VOUCHER` в `create_order()` и триггерах `trg_order_items_validate_insert` / `trg_order_items_validate_update` обязателен `denomination_id`, принадлежащий `product_id` (и активный на момент создания заказа); для `FIXED_PRODUCT` `denomination_id` запрещён (`IS NULL`).
  - `reserve_items_for_order()` проверяет лимит и резервирует по паре `(order_id, product_id, denomination_id)`.
  - `finalize_order_sale()` сверяет агрегаты `order_items` и `inventory_items (RESERVED)` по `GROUP BY product_id, denomination_id`.
  - Триггеры миграции `005` (`trg_inventory_items_validate_insert`, `trg_inventory_items_validate_update`) и `004` (`trg_order_items_prevent_delete_with_inventory`, `trg_voucher_denominations_prevent_reparent_with_order_items`) блокируют несоответствие на уровне SQL.
- **Тесты:**
  - `tests/test_phase2_security_gate_remediation.py`:
    - `test_sg_p2_001_exploit_100_try_cannot_reserve_1000_try`
    - `test_sg_p2_001_multi_denomination_order_independent_caps_and_full_lifecycle`
    - `test_sg_p2_001_create_order_denomination_validation_and_db_triggers`
    - `test_sg_p2_001_migration_004_legacy_preflight_and_clean_upgrade`
    - `test_sg_p2_001_concurrent_reservation_race_for_last_unit`
- **Воспроизведение исходного exploit:**
  Заказ на `denom_100` (`30000` коп.) -> попытка `reserve_items_for_order(order.id, voucher_prod.id, 1, denom_1000.id)` завершилась отказом (`ValidationError`), таблица `inventory_items` осталась неизменной (`CODE-1000-TRY` в статусе `AVAILABLE`).
- **Ограничения и остаточные риски (Legacy data):**
  Миграция `004` не назначает номиналы старым `VOUCHER`-заказам предположительно. Если в legacy-БД присутствуют строки `order_items` с `product_type = 'VOUCHER'` до миграции `004`, pre-flight проверка `chk_004_existing_voucher_order_item_missing_denomination` безопасно прерывает миграцию без изменения данных (требуется ручное/административное доопределение перед миграцией). Существующие `FIXED_PRODUCT` заказы мигрируют автоматически (`denomination_id IS NULL`).

---

## 2. `SG-P2-002` (HIGH) — Осиротевший инвентарь (`RESERVED`/`SOLD` с `reserved_order_id = NULL`) при удалении заказа

- **Исходная проблема:**
  В `001_initial_schema.sql` столбец `inventory_items.reserved_order_id` имел `ON DELETE SET NULL`, а `CHECK`-ограничения и триггеры не связывали `status` с `reserved_order_id`, из-за чего `DELETE FROM orders` оставлял `RESERVED`/`SOLD` позиции с `reserved_order_id = NULL`.
- **Причина:**
  Политика внешнего ключа `ON DELETE SET NULL` и отсутствие `CHECK` / триггера защиты `orders` от удаления при наличии связанного инвентаря.
- **Исправленные файлы и точные строки:**
  - `src/migrations/005_inventory_order_delete_restrict.sql:1-197`
  - `src/migrations/004_order_item_denomination.sql:79-93` (`trg_order_items_prevent_delete_with_inventory`)
- **Новое поведение:**
  - Таблица `inventory_items` пересобрана в транзакции миграции `005` с `reserved_order_id INTEGER REFERENCES orders(id) ON DELETE RESTRICT` и табличным `CHECK ((status IN ('RESERVED', 'SOLD') AND reserved_order_id IS NOT NULL) OR (status = 'AVAILABLE' AND reserved_order_id IS NULL) OR (status IN ('QUARANTINED', 'DISABLED')))`.
  - Триггер `trg_orders_prevent_delete_with_inventory` (`BEFORE DELETE ON orders`) блокирует удаление заказа при наличии любых ссылающихся `inventory_items` до каскадного удаления `order_items`.
  - `trg_inventory_items_validate_update` расширен на `UPDATE OF product_id, denomination_id, inventory_type, status, reserved_order_id`.
  - Корректный сценарий отмены заказа сохранён: `CANCELLED` -> `release_order_reservations()` сбрасывает статус в `AVAILABLE` и `reserved_order_id = NULL`.
- **Тесты:**
  - `tests/test_phase2_security_gate_remediation.py`:
    - `test_sg_p2_002_cannot_delete_order_with_reserved_or_sold_inventory`
    - `test_sg_p2_002_direct_sql_cannot_create_orphaned_reserved_or_sold`
    - `test_sg_p2_002_cancelled_order_release_lifecycle_and_safe_cleanup`
    - `test_sg_p2_002_migration_005_legacy_preflight_and_clean_upgrade`
- **Воспроизведение исходного сценария:**
  `DELETE FROM orders WHERE id = ?` при наличии `RESERVED` или `SOLD` позиций отклоняется SQLite с `sqlite3.IntegrityError`; заказ, `order_items` и инвентарь остаются нетронутыми. Прямые SQL `INSERT`/`UPDATE` с `RESERVED`/`SOLD` + `reserved_order_id = NULL` также отклоняются `sqlite3.IntegrityError`.
- **Ограничения и остаточные риски:**
  Если в legacy-БД уже имеются битые строки (`RESERVED`/`SOLD` с `reserved_order_id IS NULL`, `AVAILABLE` с не-`NULL` `reserved_order_id` или несовпадение с `order_items`), pre-flight проверки миграции `005` (`chk_005_*`) прерывают миграцию до перестройки таблицы.

---

## 3. `SG-P2-003` (HIGH) — Отсутствие проверки цены позиции заказа относительно каталога

- **Исходная проблема:**
  `OrderRepository.create_order()` проверял только сумму `total_amount_minor == sum(quantity * unit_price_minor)`, позволяя создать заказ с произвольной ценой (например, `1` коп.) или валютой.
- **Причина:**
  В `create_order()` не выполнялась сверка `unit_price_minor` и валюты заказа с `fixed_product_details.price_rub_kopecks` / `voucher_denominations.cost_rub_kopecks`.
- **Исправленные файлы и точные строки:**
  - `src/db/repositories.py:971-1143` (`OrderRepository.create_order`)
- **Новое поведение:**
  - Внутри `with transaction(self.conn, mode="IMMEDIATE"):` при создании заказа:
    - валидируется допустимость и строковый тип валюты (`RUB` для текущих каталожных цен в копейках);
    - для `FIXED_PRODUCT` цена позиции должна точно совпадать с `fixed_product_details.price_rub_kopecks` (и `approval_status == 'APPROVED'`);
    - для `VOUCHER` `denomination_id` должен принадлежать `product_id`, иметь `is_active == 1`, а `unit_price_minor` должен точно совпадать с `voucher_denominations.cost_rub_kopecks`;
    - уже созданные заказы хранят исторический snapshot `unit_price_minor` в `order_items` и не затрагиваются последующим изменением цен в каталоге.
- **Тесты:**
  - `tests/test_phase2_security_gate_remediation.py`:
    - `test_sg_p2_003_rejects_underpriced_and_overpriced_items`
    - `test_sg_p2_003_rejects_invalid_types_and_currencies`
    - `test_sg_p2_003_catalog_price_change_preserves_existing_order_snapshots`
- **Воспроизведение исходного сценария:**
  Попытка создать заказ для `FIXED_PRODUCT` (`50000` коп.) с `unit_price_minor=1` или для `denom_100` (`30000` коп.) с ценой `1` или `300000` отклоняется с `ValidationError`.
- **Ограничения и остаточные риски:**
  Текущая ценовая модель в БД хранит каталожные цены исключительно в рублёвых копейках (`price_rub_kopecks`, `cost_rub_kopecks`), поэтому создание заказа в `RUB` принудительно проверяется на этом уровне. При введении скидок/промокодов или мультивалютных цен в будущих фазах потребуется явная модель скидочных сущностей вместо обхода проверки `unit_price_minor`.


---

## 4. `SG-P2-001-FOLLOWUP` (REJECT Blocker) — Прямой SQL `UPDATE order_items` и смена `products.product_type` при наличии `order_items`

- **Статус ревизии:** Исправление подготовлено локально; ожидает повторной независимой ревизии **Argon** (PASS от имени Argon не заявляется).
- **Branch / HEAD SHA / Base:** `security/phase2-remediation` / `a7a8709d16bd8e28f9b6f49b2f0177bd3b91fef7` (base `main` и `origin/main`: `b7b0e05b36c283b07ba0fedd6bfbbc3be49a0eba`).

### Точная причина follow-up finding
1. В миграции `004_order_item_denomination.sql:51-69` триггер `trg_order_items_validate_update` срабатывал только на `BEFORE UPDATE OF product_id, product_type_snapshot, denomination_id` и не отслеживал:
   - изменение `order_id` или `quantity`;
   - привязку уже существующего `RESERVED`/`SOLD` инвентаря к кортежу `(OLD.order_id, OLD.product_id, OLD.denomination_id)`;
   - снижение совокупного `quantity` по `(OLD.order_id, OLD.product_id, OLD.denomination_id)` ниже числа уже распределённых (`RESERVED`/`SOLD`) единиц в `inventory_items`.
2. В миграциях `003` и `005` триггер `trg_products_prevent_type_change_with_inventory` проверял только `inventory_items` и `voucher_denominations`, не блокируя `UPDATE products SET product_type = ...` при наличии строк в `order_items` без инвентаря.

### Изменённые и новые файлы для закрытия `SG-P2-001-FOLLOWUP` / `SG-P2-001-DB-UPDATE`
- Синхронизированы миграции:
  - [src/migrations/003_inventory_and_integrity_guards.sql](src/migrations/003_inventory_and_integrity_guards.sql) (`trg_products_prevent_type_change_with_inventory` дополнен проверкой `order_items`)
  - [src/migrations/004_order_item_denomination.sql](src/migrations/004_order_item_denomination.sql) (`trg_order_items_validate_update` расширен на `order_id, product_id, product_type_snapshot, denomination_id, quantity` с проверками привязки `RESERVED`/`SOLD` и минимального количества)
  - [src/migrations/005_inventory_order_delete_restrict.sql](src/migrations/005_inventory_order_delete_restrict.sql) (добавлены `DROP TRIGGER IF EXISTS trg_order_items_validate_update` перед перестройкой `inventory_items`, пересоздание расширенного `trg_order_items_validate_update` и `trg_products_prevent_type_change_with_inventory` с проверкой `order_items`)
  - [src/migrations/006_order_items_update_and_product_type_guards.sql](src/migrations/006_order_items_update_and_product_type_guards.sql) (идемпотентное обновление триггеров для ранее мигрированных до `004`/`005` БД + pre-flight `chk_006_existing_order_items_quantity_below_allocated`)
- Обновлены тесты:
  - [tests/test_phase2_security_gate_remediation.py](tests/test_phase2_security_gate_remediation.py) (добавлены 5 новых методов, включая проверки на уровнях миграций `003`, `004`, `005` и `006`, все 12 обязательных сценариев `SG-P2-001-FOLLOWUP` и общим подсчётом миграций `1..6`)
  - [tests/test_phase2_audit_remediation.py](tests/test_phase2_audit_remediation.py) (ожидание `[3, 4, 5, 6]` / `6` миграций)
  - [tests/test_phase2_remediation_followup.py](tests/test_phase2_remediation_followup.py) (ожидание `[1, 2, 3, 4, 5, 6]`)
- Обновлена документация: `PHASE2_REMEDIATION_FINDINGS.md`, `PHASE2_REMEDIATION_TEST_RESULTS.txt`, `HANDOFF.md`, `project_context.md`, `AUDIT_README.md`.

### SQL-инварианты до и после исправления
- **До исправления:**
  - `trg_order_items_validate_update` в `004` проверял только `product_type_snapshot == products.product_type` и принадлежность `NEW.denomination_id` к `NEW.product_id`; прямой SQL `UPDATE order_items` позволял сменить `order_id`, `product_id`, `product_type_snapshot`, `denomination_id` (на другой валидный) или снизить `quantity` ниже числа уже `RESERVED`/`SOLD` записей в `inventory_items`.
  - `trg_products_prevent_type_change_with_inventory` в `003` и `005` пропускал `UPDATE products SET product_type = ...` при наличии `order_items`, если `inventory_items` и `voucher_denominations` для продукта ещё пусты.
- **После исправления (синхронно в `003`, `004`, `005` и `006`):**
  - В `004`, `005` (при пересоздании после перестройки `inventory_items`) и `006` `trg_order_items_validate_update` действует как `BEFORE UPDATE OF order_id, product_id, product_type_snapshot, denomination_id, quantity ON order_items`:
    - сохранены все 4 базовые проверки `004`;
    - запрещены любые изменения `order_id`, `product_id`, `product_type_snapshot` или `denomination_id` (с NULL-безопасной проверкой для `FIXED_PRODUCT`), если в `inventory_items` существуют `RESERVED`/`SOLD` строки для `(OLD.order_id, OLD.product_id, OLD.denomination_id)`;
    - запрещено уменьшение совокупного `quantity` по `(OLD.order_id, OLD.product_id, OLD.denomination_id)` (`SUM(sibling.quantity) + NEW.quantity` с исключением `OLD.id`) ниже `COUNT(*)` записей `inventory_items` со статусами `RESERVED`/`SOLD`;
    - разрешены увеличение `quantity`, уменьшение до допустимого предела `>= allocated_qty`, а также изменение независимых строк без привязанного `RESERVED`/`SOLD` инвентаря.
  - В `003`, `005` и `006` `trg_products_prevent_type_change_with_inventory` дополнен `OR EXISTS (SELECT 1 FROM order_items WHERE product_id = NEW.id)`.
  - Pre-flight `chk_006_existing_order_items_quantity_below_allocated` в `006` атомарно отклоняет апгрейд, если в существующей БД уже есть `(reserved_order_id, product_id, denomination_id)` с `COUNT(*) > SUM(order_items.quantity)`.

### Способ обновления уже применённых миграций
- Для чистых и новых БД защита действует сразу начиная с миграций `003` (`product_type`), `004` (`trg_order_items_validate_update`) и `005` (синхронное пересоздание триггеров после перестройки `inventory_items`, включая `DROP TRIGGER IF EXISTS trg_order_items_validate_update` перед `DROP TABLE inventory_items`).
- Для БД, где прежние версии `004`/`005` уже были применены ранее, миграция `006_order_items_update_and_product_type_guards.sql` пересоздаёт оба триггера через `DROP TRIGGER IF EXISTS ...; CREATE TRIGGER ...` внутри `BEGIN IMMEDIATE` транзакции после pre-flight проверки и регистрирует версию `6` в `schema_migrations`.
- Повторный вызов `run_migrations()` идемпотентен (`[]`).

### Результаты регрессионных сценариев и полного unittest suite
- **Целевые тесты (`python -m unittest -v tests/test_phase2_security_gate_remediation.py`):** **17 tests, OK** (`1.870s`), включая все 12 обязательных сценариев `SG-P2-001-FOLLOWUP`, многострочные граничные случаи, непосредственную проверку защит на уровнях `003`, `004`, `005`, пре-флайт `006` и апгрейд `005 -> 006` с проверкой целостности БД после каждого отклонённого `UPDATE`:
  1. Смена `denomination_id` после `RESERVED` — заблокировано (`sqlite3.IntegrityError`), БД неизменна.
  2. Смена `denomination_id` после `SOLD` — заблокировано, БД неизменна.
  3. Смена `product_id` (с валидной целевой парой) после `RESERVED` и `SOLD` — заблокировано, БД неизменна.
  4. Смена `order_id` и `product_type_snapshot` после `RESERVED` и `SOLD` — заблокировано, БД неизменна.
  5. Уменьшение `quantity` ниже числа `RESERVED` единиц (в т.ч. в одном multi-row `UPDATE`) — заблокировано, БД неизменна.
  6. Уменьшение `quantity` ниже числа `SOLD` единиц — заблокировано, БД неизменна.
  7. Уменьшение `quantity` до допустимой границы (`>= allocated`) — разрешено.
  8. Увеличение `quantity` — разрешено.
  9. Смена `products.product_type` при наличии `order_items` без инвентаря — заблокировано на уровнях `003`, `005` и `006`, БД неизменна (для неиспользуемого продукта без `order_items`/`inventory`/`denominations` разрешено).
  10. `FIXED_PRODUCT` с `denomination_id IS NULL` — смена `order_id`/`product_id` и снижение ниже `RESERVED` заблокированы, увеличение и допустимое уменьшение работают.
  11. Независимая неиспользованная строка в том же заказе (другой `denomination_id` без `RESERVED`/`SOLD`) изменяется свободно.
  12. Комбинированный `UPDATE` нескольких полей одновременно (`order_id + product_id + denomination_id + quantity`) после `RESERVED`/`SOLD` — заблокирован, БД неизменна.
- **Полный набор (`python -m unittest discover -v tests` -> `PHASE2_REMEDIATION_TEST_RESULTS.txt`):** **147 tests, OK** (`9.844s`, `0` failures, `0` errors) на `Python 3.14.7`, `SQLite 3.50.4`.

### Результаты adversarial-проверки
- Прямой SQL `UPDATE order_items SET order_id = <другой заказ с тем же product_id+denomination_id>` при `RESERVED` — заблокирован (`IntegrityError`).
- Выражение `UPDATE order_items SET quantity = quantity - 1` ниже числа `RESERVED` и ниже числа `SOLD` — заблокировано (`IntegrityError`).
- Одновременное изменение `product_id + product_type_snapshot + denomination_id + quantity` на `FIXED_PRODUCT` и в обратную сторону (`FIXED_PRODUCT -> VOUCHER` после `SOLD`) — заблокировано (`IntegrityError`).
- `UPDATE order_items SET quantity = quantity + 1` на соседней незарезервированной строке того же заказа — успешно без ложной блокировки.
- `UPDATE products SET product_type = 'FIXED_PRODUCT'` для `VOUCHER` с `order_items` — заблокирован (`IntegrityError`).
- `AST` по 26 `.py` файлам и SHA-256 checksums для всех 6 миграций — подтверждены.

### Оставшиеся риски и ограничения
1. Локально доступны только `Python 3.14.7` / `SQLite 3.50.4`; совместимость с `Python 3.11` и `Python 3.12` должна быть проверена в GitHub Actions CI после коммита/пуша.
2. Поля `unit_price_minor` и `orders.total_amount_minor` валидируются в `OrderRepository.create_order()` (Python), но не защищены отдельным DB-триггером от прямого SQL `UPDATE` (не входило в инварианты `SG-P2-001-FOLLOWUP`).
3. Неблокирующие `MEDIUM`/`LOW` замечания из `PHASE2_SECURITY_GATE_REPORT.md` (`SG-P2-004`–`SG-P2-016`) остаются вне scope данной задачи.

### Итоговый `git status --short` (`security/phase2-remediation`, HEAD `a7a8709d16bd8e28f9b6f49b2f0177bd3b91fef7`)
```text
 M AUDIT_README.md
 M project_context.md
 M src/db/connection.py
 M src/db/migrations.py
 M src/db/models.py
 M src/db/repositories.py
 M src/migrations/003_inventory_and_integrity_guards.sql
 M tests/test_backup.py
 M tests/test_crypto_remediation.py
 M tests/test_inventory.py
 M tests/test_orders.py
 M tests/test_phase2_audit_remediation.py
 M tests/test_receipts.py
 M tests/test_receipts_and_catalog_remediation.py
?? HANDOFF.md
?? PHASE2_REMEDIATION_FINDINGS.md
?? PHASE2_REMEDIATION_TEST_RESULTS.txt
?? PHASE2_SECURITY_GATE_REPORT.md
?? phase2_security_gate_report.txt
?? src/migrations/004_order_item_denomination.sql
?? src/migrations/005_inventory_order_delete_restrict.sql
?? src/migrations/006_order_items_update_and_product_type_guards.sql
?? tests/test_phase2_remediation_followup.py
?? tests/test_phase2_security_gate_remediation.py
```
