import asyncio
from hashlib import sha256
from io import BytesIO
from uuid import UUID

import httpx
import pytest
from PIL import Image, PngImagePlugin
from sqlalchemy import delete

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
    '''Запускаем асинхронные сценарии на asyncio.'''
    return "asyncio"


@pytest.fixture
async def client(monkeypatch, test_database_url):
    '''Запускаем API с чистой таблицей в изолированной схеме PostgreSQL.'''
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
    '''Извлекаем UUID из адреса статуса принятой загрузки.'''
    assert response.status_code == 202
    return UUID(response.headers["location"].removeprefix("/uploads/"))


async def store_accepted(response: httpx.Response) -> UUID:
    '''Выполняем транзакцию воркера для принятой загрузки.'''
    upload_id = accepted_id(response)
    await finalize_upload(app.state.db, upload_id)
    return upload_id


def image_bytes(format="PNG", size=(2, 2), pnginfo=None):
    '''Создаём JPEG или PNG заданного размера.'''
    with BytesIO() as output, Image.new("RGB", size) as image:
        image.save(output, format=format, pnginfo=pnginfo)
        return output.getvalue()


def animated_png(size=(2, 2)):
    '''Создаём PNG с двумя кадрами.'''
    with BytesIO() as output:
        with Image.new("RGB", size, "red") as first, Image.new("RGB", size, "blue") as second:
            first.save(output, format="PNG", save_all=True, append_images=[second])
        return output.getvalue()


def upload_scope(headers=()):
    '''Создаём ASGI-запрос для проверки поведения при чтении потока.'''
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "PUT",
        "scheme": "http",
        "path": "/images/photo.png",
        "root_path": "",
        "query_string": b"",
        "headers": [(b"x-api-key", b"test-key"), *headers],
    }


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("PUT", "/images/photo.png"),
        ("GET", "/images/photo.png"),
        ("GET", "/images/photo.png/thumbnail"),
        ("DELETE", "/images/photo.png"),
        ("GET", "/uploads/00000000-0000-0000-0000-000000000001"),
    ],
)
async def test_api_requires_key(client, method, path):
    '''Проверяем отказ без ключа, с неверным ключом и с хешем вместо ключа.'''
    for headers in ({}, {"X-API-Key": "wrong"}, {"X-API-Key": TEST_KEY_HASH}):
        assert (await client.request(method, path, headers=headers)).status_code == 401


@pytest.mark.parametrize(
    ("name", "original", "media_type", "thumbnail_size"),
    [
        ("photo.png", image_bytes(size=(640, 320)), "image/png", (256, 128)),
        ("photo.jpeg", image_bytes("JPEG", size=(320, 640)), "image/jpeg", (128, 256)),
        ("animation.png", animated_png((128, 64)), "image/png", (128, 64)),
    ],
)
async def test_image_lifecycle(client, name, original, media_type, thumbnail_size):
    '''Проверяем приём, статус, оригинал, миниатюру и удаление через API.'''
    url = f"/images/{name}"
    thumbnail_url = f"{url}/thumbnail"
    created = await client.put(url, headers=AUTH, content=original)
    assert created.status_code == 202
    status_url = created.headers["location"]
    assert status_url.startswith("/uploads/")

    pending = await client.get(status_url, headers=AUTH)
    assert pending.status_code == 200
    assert pending.json() == {"status": "pending", "image_url": url}
    assert pending.headers["retry-after"] == "1"
    assert pending.headers["cache-control"] == "no-store"
    assert (await client.get(url, headers=AUTH)).status_code == 404

    upload_id = await store_accepted(created)
    stored = await client.get(status_url, headers=AUTH)
    assert stored.status_code == 200
    assert stored.json() == {"status": "stored", "image_url": url}
    assert "retry-after" not in stored.headers

    response = await client.get(url, headers=AUTH)
    assert response.status_code == 200
    assert response.headers["content-type"] == media_type
    assert response.headers["content-disposition"] == f'attachment; filename="{name}"'
    assert response.content == original

    pending_thumbnail = await client.get(thumbnail_url, headers=AUTH)
    assert pending_thumbnail.status_code == 404
    assert pending_thumbnail.headers["retry-after"] == "1"
    assert pending_thumbnail.headers["cache-control"] == "no-store"
    await worker.process_thumbnail(app.state.db, upload_id)

    thumbnail = await client.get(thumbnail_url, headers=AUTH)
    assert thumbnail.status_code == 200
    assert thumbnail.headers["content-type"] == "image/png"
    assert thumbnail.headers["cache-control"] == "no-store"
    with Image.open(BytesIO(thumbnail.content)) as image:
        assert image.format == "PNG"
        assert image.size == thumbnail_size
        assert image.n_frames == 1

    await finalize_upload(app.state.db, upload_id)
    await worker.process_thumbnail(app.state.db, upload_id)
    assert (await client.get(thumbnail_url, headers=AUTH)).content == thumbnail.content

    assert (await client.delete(url, headers=AUTH)).status_code == 204
    assert (await client.get(status_url, headers=AUTH)).status_code == 404
    assert (await client.get(url, headers=AUTH)).status_code == 404
    assert (await client.delete(url, headers=AUTH)).status_code == 404
    missing_thumbnail = await client.get(thumbnail_url, headers=AUTH)
    assert missing_thumbnail.status_code == 404
    assert "retry-after" not in missing_thumbnail.headers


async def test_invalid_filename_is_rejected(client):
    '''Проверяем отказ при недопустимом имени.'''
    response = await client.put("/images/photo%5Csecret.png", headers=AUTH, content=image_bytes())
    assert response.status_code == 400


async def test_simultaneous_uploads_preserve_one_file(client):
    '''Сверяем сохранённые байты с запросом, который первым занял имя.'''
    originals = [image_bytes(size=(2, 4)), image_bytes(size=(4, 2))]
    url = "/images/photo.png"
    responses = await asyncio.gather(
        *(client.put(url, headers=AUTH, content=original) for original in originals)
    )
    codes = [response.status_code for response in responses]
    assert sorted(codes) == [202, 409]
    winner = codes.index(202)
    await store_accepted(responses[winner])
    assert (await client.get(url, headers=AUTH)).content == originals[winner]


@pytest.mark.parametrize(
    ("extension", "content"),
    [
        ("png", b""),
        ("png", image_bytes("JPEG")),
        ("png", image_bytes()[:-15]),
        ("jpg", image_bytes("JPEG")[:-2]),
    ],
    ids=["empty", "format-mismatch", "truncated-png", "truncated-jpeg"],
)
async def test_invalid_or_mismatched_image_is_rejected(client, extension, content):
    '''Проверяем ответы API для пустых, повреждённых и неверно названных файлов.'''
    response = await client.put(f"/images/photo.{extension}", headers=AUTH, content=content)
    assert response.status_code == 415


@pytest.mark.parametrize("length", ["invalid", "-1"])
async def test_invalid_content_length_is_rejected(client, length):
    '''Проверяем недопустимые значения Content-Length.'''
    headers = {**AUTH, "Content-Length": length}
    assert (await client.put("/images/photo.png", headers=headers)).status_code == 400


async def test_uploaded_image_may_take_exactly_ten_mib(client):
    '''Проверяем, что лимит размера включителен.'''
    pnginfo = PngImagePlugin.PngInfo()
    pnginfo.add(b"npAd", bytes(BYTE_LIMIT - len(image_bytes()) - 12))
    data = image_bytes(pnginfo=pnginfo)
    assert len(data) == BYTE_LIMIT
    assert (await client.put("/images/photo.png", headers=AUTH, content=data)).status_code == 202


async def test_declared_oversize_is_rejected_before_read(client):
    '''Проверяем ранний отказ по заголовку без чтения тела.'''
    async def receive():
        '''Фиксируем ошибку, если сервер пытается прочитать тело.'''
        pytest.fail("oversized request body was read")

    messages = []

    async def send(message):
        '''Сохраняем ответ приложения.'''
        messages.append(message)

    await app(upload_scope([(b"content-length", b"10485761")]), receive, send)
    assert messages[0]["status"] == 413


async def test_stream_stops_at_byte_limit(client):
    '''Проверяем лимит по реально полученным байтам при потоковой передаче.'''
    async def chunks():
        '''Передаём один лишний байт и отслеживаем дальнейшее чтение.'''
        for _ in range(10):
            yield bytes(1024 * 1024)
        yield b"x"
        pytest.fail("stream continued after exceeding the byte limit")

    response = await client.put("/images/photo.png", headers=AUTH, content=chunks())
    assert response.status_code == 413


async def test_interrupted_upload_leaves_name_available(client):
    '''После обрыва потока клиент может заново загрузить файл под тем же именем.'''
    incoming = iter([
        {"type": "http.request", "body": b"partial image", "more_body": True},
        {"type": "http.disconnect"},
    ])
    messages = []

    async def receive():
        '''Передаём фрагмент тела и событие отключения.'''
        return next(incoming)

    async def send(message):
        '''Сохраняем ответ приложения.'''
        messages.append(message)

    await app(upload_scope(), receive, send)
    assert messages[0]["status"] == 400
    assert (await client.put("/images/photo.png", headers=AUTH, content=image_bytes())).status_code == 202


async def test_animation_pixel_limit(client):
    '''Проверяем суммарный лимит пикселей для всех кадров.'''
    data = animated_png(size=(5001, 2500))
    assert len(data) < BYTE_LIMIT
    assert (await client.put("/images/photo.png", headers=AUTH, content=data)).status_code == 413


async def test_old_thumbnail_cannot_write_to_reused_name(client, monkeypatch):
    '''Удаляем оригинал, пока воркер готовит его миниатюру.'''
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
        '''Задерживаем обработку после чтения оригинала.'''
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
            finally:
                resume_render.set()

    assert (await client.get(thumbnail, headers=AUTH)).status_code == 404
    await worker.process_thumbnail(engine, new_id)
    result = await client.get(thumbnail, headers=AUTH)
    assert result.status_code == 200
    with Image.open(BytesIO(result.content)) as image:
        assert image.size == (128, 256)


async def test_thumbnail_failure_keeps_original_available(client, monkeypatch):
    '''Ошибка миниатюры не нарушает скачивание оригинала и проверку статуса.'''
    original = image_bytes()
    url = "/images/photo.png"
    created = await client.put(url, headers=AUTH, content=original)
    upload_id = await store_accepted(created)

    def invalid_image(_content):
        '''Воспроизводим окончательную ошибку декодирования.'''
        raise InvalidImage

    monkeypatch.setattr(worker, "render_thumbnail", invalid_image)
    await worker.process_thumbnail(app.state.db, upload_id)

    status = await client.get(created.headers["location"], headers=AUTH)
    assert status.json()["status"] == "stored"
    thumbnail = await client.get(f"{url}/thumbnail", headers=AUTH)
    assert thumbnail.status_code == 404
    assert "retry-after" not in thumbnail.headers
    assert (await client.get(url, headers=AUTH)).content == original


async def test_delayed_confirmation_does_not_lose_thumbnail(client):
    '''Проверяем доступность миниатюры при позднем подтверждении оригинала.'''
    engine = app.state.db
    url = "/images/photo.png"
    accepted = await client.put(url, headers=AUTH, content=image_bytes())
    upload_id = accepted_id(accepted)
    await finalize_upload(engine, upload_id)
    await mark_published(engine, upload_id, store_original=True)

    [(thumbnail_id, store_original)] = await pending_events(engine)
    assert not store_original
    await worker.process_thumbnail(engine, thumbnail_id)
    assert (await client.get(f"{url}/thumbnail", headers=AUTH)).status_code == 200


async def test_canceled_upload_cannot_store_reused_name(client):
    '''Отменённая загрузка не завершается после повторного использования имени.'''
    url = "/images/photo.png"
    canceled = await client.put(url, headers=AUTH, content=image_bytes())
    old_id = accepted_id(canceled)
    assert (await client.delete(url, headers=AUTH)).status_code == 204
    assert (await client.get(canceled.headers["location"], headers=AUTH)).status_code == 404

    original = image_bytes(size=(320, 640))
    replacement = await client.put(url, headers=AUTH, content=original)
    await finalize_upload(app.state.db, old_id)
    status = await client.get(replacement.headers["location"], headers=AUTH)
    assert status.json()["status"] == "pending"
    assert (await client.get(url, headers=AUTH)).status_code == 404
    await store_accepted(replacement)
    assert (await client.get(url, headers=AUTH)).content == original
