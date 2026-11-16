# Proposal

## Why

Клиенту нужен HTTP API для хранения изображений по имени и получения тех же файлов после перезапуска сервиса.

## What Changes

- Добавить загрузку, скачивание и удаление изображений по имени через FastAPI.
- Требовать секретный ключ для всех маршрутов API и проверять его по SHA-256-хешу из окружения.
- Запускать API и PostgreSQL через Docker Compose, хранить исходные байты в PostgreSQL и перенести файлы из прежнего тома.
- Описать запуск, команды curl и пограничные случаи в README. Проверить контракт с помощью pytest.

## Capabilities

### New Capabilities

- `image-storage` — операции над именованными изображениями, авторизация и сохранность данных после перезапуска.

### Modified Capabilities

## Impact

FastAPI, PostgreSQL, модель SQLAlchemy, миграции Alembic, Dockerfile, `docker-compose.yml`, `.env.example`, README и тесты. RabbitMQ, FastStream и уменьшенные копии составляют отдельное необязательное расширение.
