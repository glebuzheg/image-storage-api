from io import StringIO

import pytest
from alembic import command
from alembic.config import Config
from PIL import Image
from sqlalchemy import create_engine, text


def test_legacy_migration_requires_online_mode(monkeypatch):
    '''Проверяем, что офлайн SQL не отмечает импорт выполненным.'''
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://localhost/image_storage")
    config = Config("alembic.ini", output_buffer=StringIO())
    with pytest.raises(RuntimeError, match="online"):
        command.upgrade(config, "head", sql=True)


def test_legacy_import_can_retry_without_resurrecting_deleted_rows(
    empty_database_url, monkeypatch, tmp_path
):
    '''Проверяем повтор после сбоя, перенос исходных байтов и необратимость импорта.'''
    source = tmp_path / "legacy"
    monkeypatch.setenv("DATABASE_URL", empty_database_url)
    monkeypatch.setenv("LEGACY_IMAGE_DIR", str(source))
    config = Config("alembic.ini")
    command.upgrade(config, "0001_create_images")
    with pytest.raises(RuntimeError, match="LEGACY_IMAGE_DIR"):
        command.upgrade(config, "head")

    source.mkdir()
    with Image.new("RGB", (2, 2), "red") as image:
        image.save(source / "legacy.png")
    (source / "link.png").symlink_to(source / "legacy.png")
    (source / ".upload-interrupted").write_bytes(b"incomplete")
    expected = (source / "legacy.png").read_bytes()

    command.upgrade(config, "0002_import_legacy_images")
    engine = create_engine(empty_database_url)
    try:
        with engine.begin() as connection:
            rows = connection.execute(text("SELECT name, content FROM images")).all()
            assert rows == [("legacy.png", expected)]
            connection.execute(text("DELETE FROM images"))
        with pytest.raises(RuntimeError, match="cannot be downgraded"):
            command.downgrade(config, "0001_create_images")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "0002_import_legacy_images"
            )
            assert connection.scalar(text("SELECT count(*) FROM images")) == 0
        assert (source / "legacy.png").read_bytes() == expected
    finally:
        engine.dispose()


def test_thumbnail_migration_preserves_originals(empty_database_url, monkeypatch):
    '''Проверяем исходные байты до и после миграции и её отката.'''
    monkeypatch.setenv("DATABASE_URL", empty_database_url)
    monkeypatch.delenv("LEGACY_IMAGE_DIR", raising=False)
    config = Config("alembic.ini")
    command.upgrade(config, "0002_import_legacy_images")
    engine = create_engine(empty_database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text("INSERT INTO images (name, content) VALUES (:name, :content)"),
                {"name": "before.png", "content": b"legacy bytes"},
            )

        command.upgrade(config, "0003_add_image_thumbnails")
        with engine.connect() as connection:
            assert connection.execute(text(
                "SELECT name, content, upload_id, thumbnail, thumbnail_pending FROM images"
            )).all() == [("before.png", b"legacy bytes", None, None, False)]

        command.downgrade(config, "0002_import_legacy_images")
        with engine.connect() as connection:
            assert connection.execute(text("SELECT name, content FROM images")).all() == [
                ("before.png", b"legacy bytes")
            ]
    finally:
        engine.dispose()


def test_queue_migration_preserves_originals_and_pending_bytes(empty_database_url, monkeypatch):
    '''Обновляем действующие задания и сохраняем принятые байты при откате.'''
    monkeypatch.setenv("DATABASE_URL", empty_database_url)
    monkeypatch.delenv("LEGACY_IMAGE_DIR", raising=False)
    config = Config("alembic.ini")
    command.upgrade(config, "0003_add_image_thumbnails")
    engine = create_engine(empty_database_url)
    try:
        with engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO images (name, content, upload_id, thumbnail_pending) "
                "VALUES ('existing.png', :data, gen_random_uuid(), true)"
            ), {"data": b"original"})
        command.upgrade(config, "head")
        with engine.begin() as connection:
            assert connection.execute(text(
                "SELECT content, staged_content, event_pending FROM images "
                "WHERE name = 'existing.png'"
            )).one() == (b"original", None, True)
            connection.execute(text(
                "INSERT INTO images (name, staged_content, upload_id, event_pending) "
                "VALUES ('staged.png', :data, gen_random_uuid(), false)"
            ), {"data": b"staged"})
        command.downgrade(config, "0003_add_image_thumbnails")
        with engine.connect() as connection:
            assert connection.execute(text(
                "SELECT name, content, thumbnail_pending FROM images ORDER BY name"
            )).all() == [("existing.png", b"original", True), ("staged.png", b"staged", True)]
    finally:
        engine.dispose()
