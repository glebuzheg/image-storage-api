from collections.abc import Sequence
from uuid import UUID, uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from app.models import StoredImage

_thumbnail_unprocessed = StoredImage.thumbnail.is_(None) & ~StoredImage.thumbnail_failed


async def stage_upload(engine: AsyncEngine, name: str, content: bytes) -> UUID | None:
    '''Резервируем имя, сохраняем принятые байты и создаём задание на запись.'''
    statement = (
        insert(StoredImage)
        .values(name=name, staged_content=content, upload_id=uuid4(), event_pending=True)
        .on_conflict_do_nothing(index_elements=[StoredImage.name])
        .returning(StoredImage.upload_id)
    )
    async with engine.begin() as connection:
        return await connection.scalar(statement)


async def get_upload_status(engine: AsyncEngine, upload_id: UUID) -> tuple[str, bool] | None:
    '''Читаем имя и готовность оригинала без загрузки его байтов.'''
    async with engine.connect() as connection:
        result = await connection.execute(
            select(StoredImage.name, StoredImage.content.is_not(None))
            .where(StoredImage.upload_id == upload_id)
        )
        return result.tuples().one_or_none()


async def finalize_upload(engine: AsyncEngine, upload_id: UUID) -> None:
    '''Сохраняем оригинал и назначаем миниатюру одной транзакцией.'''
    async with engine.begin() as connection:
        await connection.execute(
            update(StoredImage)
            .where(StoredImage.upload_id == upload_id, StoredImage.content.is_(None))
            .values(content=StoredImage.staged_content, staged_content=None, event_pending=True)
        )


async def get_image(engine: AsyncEngine, name: str) -> bytes | None:
    '''Читаем исходные байты по первичному ключу.'''
    async with engine.connect() as connection:
        return await connection.scalar(
            select(StoredImage.content).where(StoredImage.name == name)
        )


async def delete_image(engine: AsyncEngine, name: str) -> bool:
    '''Удаляем запись и возвращаем признак того, что она существовала.'''
    async with engine.begin() as connection:
        return await connection.scalar(
            delete(StoredImage).where(StoredImage.name == name).returning(StoredImage.name)
        ) is not None


async def get_thumbnail(engine: AsyncEngine, name: str) -> tuple[bytes | None, bool] | None:
    '''Читаем миниатюру и возможность её фонового появления.'''
    async with engine.connect() as connection:
        result = await connection.execute(
            select(
                StoredImage.thumbnail, StoredImage.upload_id.is_not(None) & ~StoredImage.thumbnail_failed
            ).where(StoredImage.name == name)
        )
        return result.tuples().one_or_none()


async def pending_events(engine: AsyncEngine) -> Sequence[tuple[UUID, bool]]:
    '''Берём UUID ожидающих событий и признак этапа записи оригинала.'''
    async with engine.connect() as connection:
        result = await connection.execute(
            select(StoredImage.upload_id, StoredImage.content.is_(None))
            .where(StoredImage.event_pending)
            .order_by(StoredImage.upload_id)
            .limit(32)
        )
        return result.tuples().all()


async def mark_published(engine: AsyncEngine, upload_id: UUID, *, store_original: bool) -> None:
    '''Снимаем признак публикации только у подтверждённого этапа.'''
    stage = StoredImage.content.is_(None) if store_original else StoredImage.content.is_not(None)
    async with engine.begin() as connection:
        await connection.execute(
            update(StoredImage)
            .where(StoredImage.upload_id == upload_id, StoredImage.event_pending, stage)
            .values(event_pending=False)
        )


async def get_thumbnail_source(engine: AsyncEngine, upload_id: UUID) -> bytes | None:
    '''Читаем исходные байты только для ещё не обработанной загрузки.'''
    async with engine.connect() as connection:
        return await connection.scalar(
            select(StoredImage.content).where(
                StoredImage.upload_id == upload_id,
                _thumbnail_unprocessed,
            )
        )


async def save_thumbnail(engine: AsyncEngine, upload_id: UUID, thumbnail: bytes | None) -> None:
    '''Записываем PNG или завершаем невыполнимое задание для действующей загрузки.'''
    if thumbnail is None:
        result = {"thumbnail_failed": True, "event_pending": False}
    else:
        result = {"thumbnail": thumbnail}
    async with engine.begin() as connection:
        await connection.execute(
            update(StoredImage)
            .where(
                StoredImage.upload_id == upload_id,
                StoredImage.content.is_not(None),
                _thumbnail_unprocessed,
            )
            .values(**result)
        )
