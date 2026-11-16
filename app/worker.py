import asyncio
import logging
from collections.abc import Awaitable, Callable
from uuid import UUID

from anyio.to_thread import run_sync
from faststream import AckPolicy, FastStream
from faststream.exceptions import RejectMessage
from faststream.rabbit import Channel, RabbitBroker
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.broker import STORE_QUEUE, THUMBNAIL_QUEUE
from app.config import database_url, rabbitmq_url
from app.db import finalize_upload, get_thumbnail_source, save_thumbnail
from app.storage import InvalidImage, render_thumbnail

logger = logging.getLogger(__name__)


async def process_thumbnail(engine: AsyncEngine, upload_id: UUID) -> None:
    '''Сохраняем миниатюру действующей загрузки, если она ещё не готова.'''
    content = await get_thumbnail_source(engine, upload_id)
    if content is None:
        return
    try:
        thumbnail = await run_sync(render_thumbnail, content)
    except InvalidImage:
        logger.warning("Cannot render thumbnail for upload %s", upload_id)
        thumbnail = None
    await save_thumbnail(engine, upload_id, thumbnail)


async def main() -> None:
    '''Запускаем воркер FastStream и закрываем пул базы при остановке.'''
    engine = create_async_engine(database_url(), pool_pre_ping=True, hide_parameters=True)
    broker = RabbitBroker(
        rabbitmq_url(),
        default_channel=Channel(prefetch_count=1),
        ack_policy=AckPolicy.NACK_ON_ERROR,
    )

    async def run_job(
        message: object, process: Callable[[AsyncEngine, UUID], Awaitable[None]]
    ) -> None:
        '''Проверяем UUID и задерживаем повтор при временной ошибке.'''
        if not isinstance(message, str):
            raise RejectMessage()
        try:
            upload_id = UUID(message)
        except ValueError:
            raise RejectMessage() from None

        try:
            await process(engine, upload_id)
        except Exception:
            await asyncio.sleep(1)
            raise

    @broker.subscriber(STORE_QUEUE)
    async def store_original(message: object) -> None:
        '''Подтверждаем запись оригинала и задания на миниатюру после фиксации.'''
        await run_job(message, finalize_upload)

    @broker.subscriber(THUMBNAIL_QUEUE)
    async def create_thumbnail(message: object) -> None:
        '''Подтверждаем сохранённую миниатюру.'''
        await run_job(message, process_thumbnail)

    try:
        await FastStream(broker).run()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
