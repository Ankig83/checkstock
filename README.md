# CheckStock

Учёт складских остатков для одного магазина: где лежит товар, сколько доступно и что уже в пути или уехало к клиенту / на маркетплейс.

Веб-интерфейс и REST API. Стек: **Python**, **FastAPI**, **SQLAlchemy**, **PostgreSQL 16**.

## Возможности

- Остатки по фулфилмент-складам (физический остаток, резерв FBS, доступно)
- Сверка баланса: приход = склады + в пути + маркетплейс + «к клиенту»
- Резерв и отправка FBS
- Перемещения между складами с приёмкой
- Отгрузки FBO на склад маркетплейса
- Идемпотентные проводки (`Idempotency-Key`)
- Архивация склада без потери истории и остатков
- История операций на экранах
- Демо-данные одной кнопкой

## Быстрый старт (Docker)

Нужны Docker и Docker Compose.

```bash
docker compose up --build
```

Открыть: http://127.0.0.1:8000  

Проверка API: http://127.0.0.1:8000/api/health

Поднимаются сервисы:

| Сервис    | Описание                          |
|-----------|-----------------------------------|
| `postgres` | PostgreSQL 16 (`checkstock` / `checkstock`) |
| `app`      | FastAPI на порту `8000`           |

При первом старте создаются таблицы и демо-приход: склады Север / Юг и три артикула. Кнопка «Тестовые данные» в UI возвращает к этому состоянию.

## Локальный запуск без Docker

Нужны Python 3.11+ и PostgreSQL 16+.

Создать роль и базу:

```sql
CREATE USER checkstock WITH PASSWORD 'checkstock' CREATEDB;
CREATE DATABASE checkstock OWNER checkstock;
```

Установить зависимости и запустить:

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

export DATABASE_URL="postgresql+psycopg://checkstock:checkstock@localhost:5432/checkstock"
# Windows PowerShell:
# $env:DATABASE_URL = "postgresql+psycopg://checkstock:checkstock@localhost:5432/checkstock"

uvicorn app.main:app --reload
```

Если порт `5432` занят, укажите свой хост/порт в `DATABASE_URL`. Пример переменных — в `.env.example`.

## Конфигурация

| Переменная         | По умолчанию | Описание |
|--------------------|--------------|----------|
| `DATABASE_URL`     | `postgresql+psycopg://checkstock:checkstock@localhost:5432/checkstock` | Строка подключения SQLAlchemy |
| `SEED_ON_STARTUP`  | `true`       | Сид демо-данных при старте |

## Тесты

Тесты используют отдельную базу `checkstock_test` на том же PostgreSQL и не очищают рабочую базу.

```bash
pytest
```

Перед запуском должен быть доступен PostgreSQL (например, `docker compose up -d postgres`).

## Структура проекта

```
app/           # FastAPI-приложение, модели, складская логика, API
static/        # UI (HTML / CSS / JS)
tests/         # pytest
Dockerfile
docker-compose.yml
requirements.txt
```

## Учётная модель (кратко)

- На ФФ хранятся **физический остаток** и **резерв FBS**; **доступно** = физический − резерв.
- В сверке резерв не плюсуется второй раз: он уже входит в физический остаток.
- Перемещение, FBO и отправка клиенту оформляются **документами** (отправка → в пути → приёмка).
- Нехватка считается **только по выбранному складу**; с другого склада количество само не снимается.
- Удаление склада — смена статуса на **архив**, не физическое удаление строк.
- Параллельные списания защищены `SELECT … FOR UPDATE`; идемпотентность — через `Idempotency-Key` и advisory lock в PostgreSQL.
- Схема создаётся при старте (`create_all`); отдельного справочника товаров нет — артикулы задаются демо-приходом.
