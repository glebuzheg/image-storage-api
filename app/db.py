from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from app.models import StoredImage


async def insert_image(engine: AsyncEngine, name: str, content: bytes) -> bool:
    '''Атомарно занимаем имя и фиксируем байты в одной транзакции.'''
    statement = (
        insert(StoredImage)
        .values(name=name, content=content)
        .on_conflict_do_nothing(index_elements=[StoredImage.name])
        .returning(StoredImage.name)
    )
    async with engine.begin() as connection:
        return await connection.scalar(statement) is not None


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
