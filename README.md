# Image Storage API

FastAPI-сервис для хранения JPEG и PNG в PostgreSQL. Воркер сохраняет оригиналы и создаёт миниатюры по заданиям из RabbitMQ.

## Запуск

Из корня проекта с Docker Compose:

```sh
cp .env.example .env
docker compose up
```

| Переменная | Назначение |
| --- | --- |
| `API_KEY_HASH` | SHA-256 ключа клиента в hex. В примере используется ключ `local-dev-key`. |
| `POSTGRES_PASSWORD` | Пароль пользователя PostgreSQL `image_storage`. |
| `RABBITMQ_PASSWORD` | Пароль пользователя RabbitMQ `image_storage`. |
| `API_PORT` | Порт на `127.0.0.1`, по умолчанию `8000`. |

Обычный `docker compose down` сохраняет данные PostgreSQL и очереди RabbitMQ. Для внешнего доступа задайте собственные ключ и пароли и используйте HTTPS.

## API

Все запросы требуют `X-API-Key`. Тело загрузки содержит байты JPEG или PNG. Имя соответствует `[a-z0-9][a-z0-9_-]{0,63}\.(jpg|jpeg|png)`. Лимиты — 10 МиБ и 25 млн пикселей суммарно по кадрам.

| Запрос | Ответ |
| --- | --- |
| `PUT /images/{name}` | `202 Accepted`, в `Location` указан адрес статуса. |
| `GET /uploads/{uuid}` | `200 OK` с `status` (`pending` или `stored`) и `image_url`. |
| `GET /images/{name}` | `200 OK` с оригиналом после записи. |
| `GET /images/{name}/thumbnail` | `200 OK` с PNG до 256 × 256. Пока миниатюра готовится, возвращает `404` с `Retry-After: 1`. |
| `DELETE /images/{name}` | `204 No Content` после отмены загрузки или удаления оригинала и миниатюры. |

Коды ошибок — `400` (имя или `Content-Length`), `401` (ключ), `404` (ресурс недоступен), `409` (имя занято), `413` (превышен лимит), `415` (недопустимый файл), `422` (некорректный UUID).

```sh
base=http://127.0.0.1:8000
key=local-dev-key
url="$base/images/photo.png"

status_path=$(curl -fsS -D - -o /dev/null -H "X-API-Key: $key" --upload-file photo.png "$url" |
  awk 'tolower($1) == "location:" { sub(/\r$/, "", $2); print $2 }')
curl -fsS -H "X-API-Key: $key" "$base$status_path"
# Дождитесь stored и готовности миниатюры.
curl -f -H "X-API-Key: $key" "$url" -o downloaded.png
curl -f -H "X-API-Key: $key" "$url/thumbnail" -o thumbnail.png
curl -f -H "X-API-Key: $key" -X DELETE "$url"
```

## Тесты

Задайте `TEST_DATABASE_URL` для тестового PostgreSQL и запустите `uv run --locked pytest` с Python 3.13. Тесты создают временную схему.

[Архитектура](ARCHITECTURE.md) · [Спецификации OpenSpec](openspec/specs/)
