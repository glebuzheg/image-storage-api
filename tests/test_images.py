import asyncio
from hashlib import sha256
from io import BytesIO
from tempfile import TemporaryFile
from uuid import UUID
from zlib import crc32

import httpx
import pytest
from PIL import Image, PngImagePlugin
from sqlalchemy import delete, func, select, update

from app import worker
from app.db import finalize_upload, mark_published, pending_events
from app.main import app
from app.models import StoredImage
from app.storage import InvalidImage

pytestmark = pytest.mark.anyio
AUTH = {"X-API-Key": "test-key"}
TEST_KEY_HASH = sha256(b"test-key").hexdigest()
BYTE_LIMIT = 10 * 1024 * 1024


@pytest.fixture
def anyio_backend():
    '''Фиксируем asyncio, который используется и в конкурентных тестах.'''
    return "asyncio"


@pytest.fixture
async def client(monkeypatch, test_database_url):
    '''Запускаем приложение с чистой таблицей в изолированной тестовой схеме.'''
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.setenv("API_KEY_HASH", TEST_KEY_HASH)
    monkeypatch.setenv("DATABASE_URL", test_database_url)
    monkeypatch.setenv("RABBITMQ_URL", "amqp://127.0.0.1:1/")
    async with app.router.lifespan_context(app):
        async with app.state.db.begin() as connection:
            await connection.execute(delete(StoredImage))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client


def accepted_id(response: httpx.Response) -> UUID:
    '''Извлекаем идентификатор из адреса статуса принятой загрузки.'''
    assert response.status_code == 202
    return UUID(response.headers["location"].removeprefix("/uploads/"))


async def store_accepted(response: httpx.Response) -> UUID:
    '''Выполняем транзакцию воркера без транспорта RabbitMQ.'''
    upload_id = accepted_id(response)
    await finalize_upload(app.state.db, upload_id)
    return upload_id


async def image_count() -> int:
    '''Считаем строки таблицы, чтобы проверить побочные эффекты запросов.'''
    async with app.state.db.connect() as connection:
        return await connection.scalar(select(func.count()).select_from(StoredImage))


def image_bytes(format="PNG", color="red", size=(2, 2), pnginfo=None):
    '''Готовим настоящие изображения с управляемым форматом и размером.'''
    output = BytesIO()
    with Image.new("RGB", size, color) as image:
        image.save(output, format=format, pnginfo=pnginfo)
    return output.getvalue()


def animated_png(size=(2, 2)):
    '''Создаём два различимых кадра для тестов анимации.'''
    output = BytesIO()
    with Image.new("RGB", size, "red") as first, Image.new("RGB", size, "blue") as second:
        first.save(output, format="PNG", save_all=True, append_images=[second])
    return output.getvalue()


def png_with_corrupt_second_frame():
    '''Портим второй кадр с сохранением корректной контрольной суммы блока.'''
    data = animated_png()
    chunk = data.index(b"fdAT")
    size = int.from_bytes(data[chunk - 4:chunk])
    end = chunk + 4 + size
    replacement = data[chunk:chunk + 8] + bytes(size - 4)
    checksum = crc32(replacement).to_bytes(4)
    return data[:chunk] + replacement + checksum + data[end + 4:]


def image_scope(headers=(), method="PUT"):
    '''Собираем ASGI-запрос для контроля чтения тела и отправки ответа.'''
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": "/images/photo.png",
        "root_path": "",
        "query_string": b"",
        "headers": [(b"x-api-key", b"test-key"), *headers],
    }


@pytest.mark.parametrize("key_hash", [None, "", "  ", "a" * 63, "g" * 64])
async def test_startup_requires_key_hash(monkeypatch, key_hash):
    '''Проверяем формат хеша и отказ от устаревшей настройки с исходным ключом.'''
    monkeypatch.setenv("API_KEY", "test-key")
    if key_hash is None:
        monkeypatch.delenv("API_KEY_HASH", raising=False)
    else:
        monkeypatch.setenv("API_KEY_HASH", key_hash)
    with pytest.raises(RuntimeError, match="API_KEY_HASH"):
        async with app.router.lifespan_context(app):
            pytest.fail("startup accepted missing or invalid API_KEY_HASH")


async def test_startup_requires_database_configuration(monkeypatch):
    '''Проверяем отказ запуска при отсутствии настроек PostgreSQL.'''
    monkeypatch.setenv("API_KEY_HASH", TEST_KEY_HASH)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    with pytest.raises(RuntimeError, match="POSTGRES_PASSWORD"):
        async with app.router.lifespan_context(app):
            pytest.fail("startup accepted missing database settings")


@pytest.mark.parametrize("method", ["PUT", "GET", "DELETE"])
@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"}, {"X-API-Key": TEST_KEY_HASH}])
async def test_all_image_routes_require_key(client, method, headers):
    '''Проверяем общий барьер авторизации на всех трёх операциях.'''
    response = await client.request(method, "/images/photo.png", headers=headers)
    assert response.status_code == 401


async def test_missing_key_is_rejected_with_empty_key_hash(monkeypatch, test_database_url):
    '''Проверяем обязательный заголовок даже при хеше пустой строки в настройках.'''
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.setenv("API_KEY_HASH", sha256(b"").hexdigest())
    monkeypatch.setenv("DATABASE_URL", test_database_url)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.get("/images/photo.png")).status_code == 401


async def test_builtin_api_docs_are_disabled(client):
    '''Проверяем, что встроенные маршруты документации отключены.'''
    for route in ("/docs", "/redoc", "/openapi.json"):
        assert (await client.get(route)).status_code == 404


@pytest.mark.parametrize(
    ("name", "format", "media_type"),
    [("photo.png", "PNG", "image/png"), ("photo.jpeg", "JPEG", "image/jpeg")],
)
async def test_upload_download_delete(client, name, format, media_type):
    '''Проверяем полный цикл и побайтовую сохранность JPEG и PNG.'''
    data = image_bytes(format)
    url = f"/images/{name}"
    created = await client.put(url, headers=AUTH, content=data)
    assert created.status_code == 202

    status_url = created.headers["location"]
    assert status_url.startswith("/uploads/")
    assert (await client.get(status_url)).status_code == 401
    pending = await client.get(status_url, headers=AUTH)
    assert pending.status_code == 200
    assert pending.json() == {"status": "pending", "image_url": url}
    assert pending.headers["retry-after"] == "1"
    assert pending.headers["cache-control"] == "no-store"
    assert (await client.get(url, headers=AUTH)).status_code == 404

    await store_accepted(created)
    stored = await client.get(status_url, headers=AUTH)
    assert stored.status_code == 200
    assert stored.json() == {"status": "stored", "image_url": url}
    assert "retry-after" not in stored.headers

    downloaded = await client.get(url, headers=AUTH)
    assert downloaded.status_code == 200
    assert downloaded.headers["content-type"] == media_type
    assert downloaded.headers["content-disposition"] == f'attachment; filename="{name}"'
    assert downloaded.content == data

    deleted = await client.delete(url, headers=AUTH)
    assert deleted.status_code == 204
    assert deleted.content == b""
    assert (await client.get(status_url, headers=AUTH)).status_code == 404
    assert (await client.get(url, headers=AUTH)).status_code == 404
    assert (await client.delete(url, headers=AUTH)).status_code == 404


@pytest.mark.parametrize("method", ["PUT", "GET", "DELETE"])
async def test_invalid_filename_is_rejected(client, method):
    '''Проверяем, что недопустимое имя не создаёт запись в PostgreSQL.'''
    response = await client.request(method, "/images/photo%5Csecret.png", headers=AUTH)
    assert response.status_code == 400
    assert await image_count() == 0


async def test_simultaneous_uploads_preserve_one_file(client):
    '''Сверяем сохранённые байты с запросом, который первым занял имя.'''
    data = [image_bytes(color="red"), image_bytes(color="blue")]
    url = "/images/photo.png"
    responses = await asyncio.gather(
        *(client.put(url, headers=AUTH, content=body) for body in data)
    )
    codes = [response.status_code for response in responses]
    assert sorted(codes) == [202, 409]
    await store_accepted(responses[codes.index(202)])
    assert (await client.get(url, headers=AUTH)).content == data[codes.index(202)]
    assert await image_count() == 1


@pytest.mark.parametrize(
    ("extension", "content"),
    [
        ("png", b""),
        ("png", b"not an image"),
        ("png", image_bytes("JPEG")),
        ("png", image_bytes()[:-15]),
        ("png", image_bytes()[:-4]),
        ("png", image_bytes()[:-1] + b"\0"),
        ("png", image_bytes()[:33] + image_bytes()[-12:]),
        ("png", png_with_corrupt_second_frame()),
        ("jpg", image_bytes("JPEG")[:-2]),
    ],
)
async def test_invalid_or_mismatched_image_is_rejected(client, extension, content):
    '''Проверяем, что повреждения или подмена формата не создают доступный файл.'''
    response = await client.put(f"/images/photo.{extension}", headers=AUTH, content=content)
    assert response.status_code == 415
    assert await image_count() == 0


async def test_uploaded_image_may_take_exactly_ten_mib(client):
    '''Фиксируем, что верхняя граница размера включительна.'''
    pnginfo = PngImagePlugin.PngInfo()
    pnginfo.add(b"npAd", bytes(BYTE_LIMIT - len(image_bytes()) - 12))
    data = image_bytes(pnginfo=pnginfo)
    assert len(data) == BYTE_LIMIT
    response = await client.put("/images/photo.png", headers=AUTH, content=data)
    assert response.status_code == 202


@pytest.mark.parametrize(
    ("name", "declared_size", "key_headers", "code", "detail"),
    [
        ("photo.png", "invalid", AUTH, 400, "Invalid Content-Length"),
        ("photo.png", "-1", AUTH, 400, "Invalid Content-Length"),
        ("PHOTO.png", "10485761", AUTH, 400, "Invalid image name"),
        ("PHOTO.png", "10485761", {}, 401, "Invalid or missing API key"),
    ],
)
async def test_upload_metadata_validation_precedence(
    client, name, declared_size, key_headers, code, detail
):
    '''Проверяем порядок отказов и отсутствие записи в хранилище.'''
    response = await client.put(
        f"/images/{name}", headers={**key_headers, "Content-Length": declared_size}
    )
    assert (response.status_code, response.json()) == (code, {"detail": detail})
    assert await image_count() == 0


async def test_declared_oversize_is_rejected_before_read(client):
    '''Проверяем ранний отказ по заголовку без чтения тела.'''
    async def receive():
        '''Любое чтение тела в этом сценарии означает регрессию.'''
        pytest.fail("oversized request body was read")

    messages = []

    async def send(message):
        '''Фиксируем HTTP-статус, который приложение отправляет по ASGI.'''
        messages.append(message)

    await app(image_scope([(b"content-length", b"10485761")]), receive, send)
    assert messages[0]["status"] == 413
    assert await image_count() == 0


async def test_stream_stops_as_soon_as_it_exceeds_limit(client):
    '''Проверяем лимит по реально полученным байтам при потоковой передаче.'''
    async def chunks():
        '''Передаём один лишний байт и отслеживаем попытку дальнейшего чтения.'''
        for _ in range(10):
            yield bytes(1024 * 1024)
        yield b"x"
        pytest.fail("stream continued after exceeding the byte limit")

    response = await client.put("/images/photo.png", headers=AUTH, content=chunks())
    assert response.status_code == 413
    assert await image_count() == 0


async def test_disconnect_cleans_up_temporary_file(client, monkeypatch):
    '''Проверяем закрытие временного файла и отсутствие строки после обрыва.'''
    opened = []

    def record_file():
        '''Запоминаем настоящий временный файл для проверки закрытия.'''
        source = TemporaryFile()
        opened.append(source)
        return source

    monkeypatch.setattr("app.storage.TemporaryFile", record_file)
    incoming = iter([
        {"type": "http.request", "body": b"partial image", "more_body": True},
        {"type": "http.disconnect"},
    ])

    async def receive():
        '''Последовательно передаём фрагмент тела и событие разрыва.'''
        return next(incoming)

    async def send(message):
        '''Игнорируем ответы после обрыва, поскольку клиент уже отключился.'''
        pass

    await app(image_scope(), receive, send)
    assert len(opened) == 1 and opened[0].closed
    assert await image_count() == 0


async def test_pixel_limit_applies_to_a_small_file(client):
    '''Проверяем лимит пикселей на хорошо сжимаемом изображении.'''
    data = image_bytes(size=(5001, 5000))
    assert len(data) < BYTE_LIMIT
    response = await client.put("/images/photo.png", headers=AUTH, content=data)
    assert response.status_code == 413
    assert await image_count() == 0


async def test_valid_animation_round_trips(client):
    '''Проверяем, что допустимая анимация возвращается без изменения байтов.'''
    url = "/images/photo.png"
    data = animated_png()
    await store_accepted(await client.put(url, headers=AUTH, content=data))
    assert (await client.get(url, headers=AUTH)).content == data


async def test_animation_pixel_limit(client):
    '''Два кадра по отдельности допустимы, но их сумма превышает лимит.'''
    data = animated_png(size=(5001, 2500))
    assert len(data) < BYTE_LIMIT
    response = await client.put("/images/photo.png", headers=AUTH, content=data)
    assert response.status_code == 413
    assert await image_count() == 0


async def test_started_download_survives_delete(client):
    '''Проверяем выдачу считанных байтов после удаления строки.'''
    data = image_bytes()
    url = "/images/photo.png"
    await store_accepted(await client.put(url, headers=AUTH, content=data))
    messages = []

    async def receive():
        '''Задаём пустое тело GET для прямого вызова ASGI.'''
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        '''Удаляем имя сразу после начала ответа, пока тело ещё не отправлено.'''
        messages.append(message)
        if message["type"] == "http.response.start":
            assert (await client.delete(url, headers=AUTH)).status_code == 204

    await app(image_scope(method="GET"), receive, send)
    assert messages[0]["status"] == 200
    assert b"".join(msg["body"] for msg in messages[1:]) == data


async def test_thumbnail_http_contract(client):
    '''Проверяем защиту, валидацию имени и статусы отсутствующей миниатюры.'''
    url = "/images/photo.png"
    thumbnail = f"{url}/thumbnail"
    assert (await client.get(thumbnail)).status_code == 401
    assert (await client.get("/images/PHOTO.png/thumbnail", headers=AUTH)).status_code == 400
    missing = await client.get(thumbnail, headers=AUTH)
    assert missing.status_code == 404
    assert missing.headers["cache-control"] == "no-store"
    assert "retry-after" not in missing.headers

    async with app.state.db.begin() as connection:
        await connection.execute(
            StoredImage.__table__.insert().values(name="legacy.png", content=b"legacy bytes")
        )
    legacy = await client.get("/images/legacy.png/thumbnail", headers=AUTH)
    assert legacy.status_code == 404 and "retry-after" not in legacy.headers
    assert (await client.get("/images/legacy.png", headers=AUTH)).content == b"legacy bytes"

    original = image_bytes(size=(640, 320))
    created = await client.put(url, headers=AUTH, content=original)
    assert created.status_code == 202
    pending = await client.get(thumbnail, headers=AUTH)
    assert pending.status_code == 404
    assert pending.headers["retry-after"] == "1"
    assert pending.headers["cache-control"] == "no-store"
    await store_accepted(created)
    assert (await client.get(url, headers=AUTH)).content == original


@pytest.mark.parametrize(
    ("name", "original", "expected_size"),
    [
        ("photo.jpg", image_bytes("JPEG", size=(640, 320)), (256, 128)),
        ("photo.png", animated_png(size=(128, 64)), (128, 64)),
    ],
)
async def test_worker_creates_thumbnail_once(client, name, original, expected_size):
    '''Проверяем формат, размеры, повтор и удаление результата.'''
    engine = app.state.db
    url = f"/images/{name}"
    upload_id = await store_accepted(await client.put(url, headers=AUTH, content=original))
    assert (await client.put(url, headers=AUTH, content=original)).status_code == 409
    await worker.process_thumbnail(engine, upload_id)

    result = await client.get(f"{url}/thumbnail", headers=AUTH)
    assert result.status_code == 200
    assert result.headers["content-type"] == "image/png"
    assert result.headers["cache-control"] == "no-store"
    with Image.open(BytesIO(result.content)) as image:
        assert image.format == "PNG"
        assert image.size == expected_size
        assert image.n_frames == 1

    async with engine.begin() as connection:
        await connection.execute(
            update(StoredImage).where(StoredImage.upload_id == upload_id)
            .values(thumbnail=b"previous result")
        )
    await worker.process_thumbnail(engine, upload_id)
    assert (await client.get(f"{url}/thumbnail", headers=AUTH)).content == b"previous result"
    assert (await client.get(url, headers=AUTH)).content == original
    assert (await client.delete(url, headers=AUTH)).status_code == 204
    assert await pending_events(engine) == []
    assert (await client.get(f"{url}/thumbnail", headers=AUTH)).status_code == 404


async def test_deleted_upload_cannot_write_to_reused_name(client, monkeypatch):
    '''Удаляем оригинал между чтением и записью воркера и повторно занимаем имя.'''
    engine = app.state.db
    url = "/images/photo.png"
    thumbnail = f"{url}/thumbnail"
    old_id = await store_accepted(
        await client.put(url, headers=AUTH, content=image_bytes(size=(640, 320)))
    )
    render_started = asyncio.Event()
    resume_render = asyncio.Event()
    run_sync = worker.run_sync

    async def pause_render(func, content):
        '''Приостанавливаем старое задание после чтения оригинала.'''
        render_started.set()
        await resume_render.wait()
        return await run_sync(func, content)

    with monkeypatch.context() as patch:
        patch.setattr(worker, "run_sync", pause_render)
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(worker.process_thumbnail(engine, old_id))
            try:
                await asyncio.wait_for(render_started.wait(), timeout=3)
                assert (await client.delete(url, headers=AUTH)).status_code == 204
                new_id = await store_accepted(await client.put(
                    url, headers=AUTH, content=image_bytes(size=(320, 640))
                ))
                assert new_id != old_id
            finally:
                resume_render.set()

    await worker.process_thumbnail(engine, old_id)
    pending = await client.get(thumbnail, headers=AUTH)
    assert pending.status_code == 404
    assert pending.headers["retry-after"] == "1"
    await worker.process_thumbnail(engine, new_id)
    result = await client.get(thumbnail, headers=AUTH)
    assert result.status_code == 200
    with Image.open(BytesIO(result.content)) as image:
        assert image.size == (128, 256)


async def test_permanent_render_failure_ends_job(client, monkeypatch):
    '''Завершаем задание при ошибке Pillow и оставляем оригинал доступным.'''
    original = image_bytes()
    url = "/images/photo.png"
    created = await client.put(url, headers=AUTH, content=original)
    upload_id = await store_accepted(created)

    def invalid_image(_content):
        '''Воспроизводим окончательную ошибку декодирования.'''
        raise InvalidImage

    monkeypatch.setattr(worker, "render_thumbnail", invalid_image)
    await worker.process_thumbnail(app.state.db, upload_id)
    assert await pending_events(app.state.db) == []
    assert (await client.get(created.headers["location"], headers=AUTH)).json()["status"] == "stored"
    response = await client.get(f"{url}/thumbnail", headers=AUTH)
    assert response.status_code == 404 and "retry-after" not in response.headers
    assert (await client.get(url, headers=AUTH)).content == original


async def test_delayed_store_confirmation_preserves_thumbnail_event(client):
    '''Подтверждаем оригинал после того, как воркер уже поставил миниатюру.'''
    engine = app.state.db
    url = "/images/photo.png"
    original = image_bytes()
    accepted = await client.put(url, headers=AUTH, content=original)
    upload_id = accepted_id(accepted)
    assert await pending_events(engine) == [(upload_id, True)]
    assert (await client.put(url, headers=AUTH, content=original)).status_code == 409

    await finalize_upload(engine, upload_id)
    await mark_published(engine, upload_id, store_original=True)
    assert await pending_events(engine) == [(upload_id, False)]

    await mark_published(engine, upload_id, store_original=False)
    await finalize_upload(engine, upload_id)
    assert await pending_events(engine) == []
    assert (await client.get(url, headers=AUTH)).content == original


async def test_delete_cancels_original_and_old_event_cannot_store_reused_name(client):
    '''Старый UUID остаётся недействительным после отмены и повторной загрузки.'''
    url = "/images/photo.png"
    accepted = await client.put(url, headers=AUTH, content=image_bytes())
    old_id = accepted_id(accepted)
    assert (await client.delete(url, headers=AUTH)).status_code == 204
    assert (await client.get(accepted.headers["location"], headers=AUTH)).status_code == 404
    assert await pending_events(app.state.db) == []

    new_content = image_bytes(size=(320, 640))
    replacement = await client.put(url, headers=AUTH, content=new_content)
    new_id = accepted_id(replacement)
    assert new_id != old_id
    await finalize_upload(app.state.db, old_id)
    assert (await client.get(url, headers=AUTH)).status_code == 404
    await finalize_upload(app.state.db, new_id)
    assert (await client.get(url, headers=AUTH)).content == new_content
