# Сервис для PS (PlayStation Digital Vouchers & Products Core)

Ядро сервиса подбора, учёта, хранения и транзакционной обработки цифровых ваучеров и цифровых товаров для PlayStation Store (текущая готовность: **Phase 1 + Phase 2 Security Gate Remediation**).

> **Важно:** Интеграция Telegram-бота (`aiogram`), веб-витрины (Mini App), реальных платёжных провайдеров и автоматической выдачи товаров **не реализована** (запрещена до прохождения независимого аудита Security Gate Phase 2). См. [AUDIT_README.md](AUDIT_README.md).

## Структура репозитория

- `src/engine/` — математический движок оптимального подбора номиналов ваучеров (Bounded Covering Knapsack, целочисленная арифметика в минорных единицах и копейках).
- `src/calculator.py` — фасад верхнего уровня над `src/engine`.
- `src/db/` — слой работы с SQLite:
  - `connection.py` — создание подключений (WAL, `foreign_keys = ON`, `busy_timeout`), онлайн-бэкап и безопасное восстановление БД (`backup_database`, `restore_database`).
  - `crypto.py` — Fernet-шифрование кодов инвентаря, HMAC-SHA256 fingerprint, SHA-256 хеширование чеков, политика запрета fallback-секретов вне разрешённых dev/test окружений.
  - `migrations.py` — атомарный запуск SQL-миграций в `BEGIN IMMEDIATE; ... COMMIT;`.
  - `models.py` — доменные модели, перечисления состояний и исключения.
  - `repositories.py` — репозитории (`CatalogRepository`, `UserRepository`, `InventoryRepository`, `OrderRepository`, `ReceiptRepository`).
- `src/migrations/` — SQL-миграции схемы (`001_initial_schema.sql`, `002_provider_scoped_receipts.sql`).
- `tests/` — модульные, конкурентные и регрессионные тесты (`unittest`).
- `project.md`, `project_context.md`, `AVITO_TRAFFIC_ENGINE.md` — продуктовая и архитектурная документация.
- `AUDIT_README.md` — специализированное руководство для независимого аудитора.

## Требования и установка зависимостей

- Python `3.11+` (фактически протестировано на `Python 3.14.7`)
- Виртуальное окружение (рекомендуется)

```bash
python -m venv .venv
# Windows PowerShell:
.venv\Scripts\Activate.ps1
# Linux/macOS:
# source .venv/bin/activate

pip install -r requirements.txt
```

## Переменные окружения

Скопируйте шаблон `.env.example` при необходимости локального запуска (не коммитьте `.env`):

- `ENV` / `APP_ENV` — режим окружения. Fallback-ключи для тестов/разработки разрешаются только если задана хотя бы одна переменная и **все** заданные значения принадлежат `{test, testing, dev, development}`.
- `INVENTORY_ENCRYPTION_KEY` — 32-байтовый url-safe base64 ключ Fernet (обязателен вне разрешённых dev/test сред).
- `FINGERPRINT_PEPPER` (или `INVENTORY_FINGERPRINT_PEPPER`) — секретный перец для HMAC-SHA256 (обязателен вне разрешённых dev/test сред).

## Запуск тестов

```bash
python -m unittest discover -v tests
```

На текущем состоянии кодовой базы все **107 тестов** проходят успешно (`OK`).

## Создание тестовой базы и применение миграций

```bash
python -c "from src.db import create_connection, run_migrations; c = create_connection('data/dev_test.db'); print('Applied:', run_migrations(c)); c.close()"
```

По умолчанию `create_connection()` без аргумента использует `Path("data/ps_service.db")`. Директория `data/` и файлы `*.db*` исключены из Git через `.gitignore`.
