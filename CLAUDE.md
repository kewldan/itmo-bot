# CLAUDE.md

Telegram-бот мониторинга конкурсных списков abit.itmo.ru (см. README.md).
Общение и тексты бота — на русском.

## Инструменты — строго

- Пакеты и окружение — только **uv** (`uv sync`, `uv run`, `uv add`).
  Никаких pip/poetry. Зависимости — в `pyproject.toml`, лок — `uv.lock`.
- Линт — **ruff** с `select = ["ALL"]`. Запрещены: `# noqa`, добавление
  правил в `ignore` (там только пара, официально несовместимая
  с `ruff format`). Кириллица в текстах решается через
  `allowed-confusables`, не отключением RUF001-003. Единственные
  per-file-ignores — pytest-идиомы для `tests/**` (S101, PLR2004,
  SLF001); на `app/` исключений нет.
- Типы — **ty**. Запрещён `# type: ignore`.
- Перед завершением любой правки: `uv run ruff format app tests &&
  uv run ruff check app tests && uv run ty check app tests &&
  uv run pytest -q`. CI (GitHub Actions) гоняет то же самое.

## Стек и архитектурные решения

- **aiogram 3**: DI через `dispatcher["db"] = Database(...)` — хендлеры
  получают `db: Database` параметром. FSM-storage: Redis/Valkey, если задан
  `REDIS_URL`, иначе память.
- **БД — raw asyncpg без ORM** (`app/db.py`): весь SQL в одном классе
  `Database`, схема — `CREATE TABLE IF NOT EXISTS` при старте, json/jsonb
  кодеки ставятся в `init` пула. Колонки в SELECT перечисляются явно
  (f-строки в SQL запрещены — S608).
- Снапшоты списков хранятся целиком (компактные записи `CompactItem`,
  JSONB) — модель и графики пересчитываются по истории.
- Модель вероятности — `app/metrics.py`, подробный докстринг модуля.
  Параметры зависят от вида финансирования (`contract` / `budget`);
  бюджетная оценка помечается `approximate`.
- Графики — matplotlib + Agg (бэкенд через `MPLBACKEND` в `app/__init__.py`,
  не `matplotlib.use()`). Оси времени — `mdates.date2num` (стабы `Axes.plot`
  не принимают `list[datetime]`).
- Поллер (`app/poller.py`) и кросс-обход (`app/cross.py`) не должны
  умирать: циклы ловят сбои через
  `asyncio.gather(..., return_exceptions=True)`, а не `except Exception`
  (BLE001).
- Фоновые задачи (поллер, кросс-обход, бэкап) стартуют в `main.py`
  и отменяются в `finally`.
- Кросс-анализ: слепки чужих программ хранятся по одному на программу
  (`cross_lists`), эффективные приоритеты считаются на лету
  в `app/cross.py::effective_priorities`.

## Проверка без Docker

Docker на машине разработки может отсутствовать. Слой БД проверяется
встроенным PostgreSQL (колёса pgserver есть только до Python 3.12):

```bash
uv run --no-project --python 3.12 \
  --with pgserver,asyncpg,httpx,pydantic-settings python <скрипт>
```

Фетчер/модель/графики проверяются на живых данных без БД — сайт отдаёт
полный список в `__NEXT_DATA__` без авторизации.

## Деплой

`docker compose up -d --build` — бот + PostgreSQL 17 + Valkey 8.
Dockerfile собирается через uv (`uv sync --frozen --no-dev`).
