import asyncio
import logging

from faststream.rabbit import Channel, RabbitBroker, RabbitQueue
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db import mark_published, pending_events

STORE_QUEUE = RabbitQueue("image.store", durable=True)
THUMBNAIL_QUEUE = RabbitQueue("image.thumbnail", durable=True)
logger = logging.getLogger(__name__)


async def publish_pending(engine: AsyncEngine, url: str) -> None:
    '''Публикуем задания из PostgreSQL и повторяем отправку после сбоя.'''
    while True:
        try:
            async with RabbitBroker(
                url, timeout=5, default_channel=Channel(on_return_raises=True)
            ) as broker:
                for queue in (STORE_QUEUE, THUMBNAIL_QUEUE):
                    await asyncio.wait_for(broker.declare_queue(queue), timeout=5)
                while True:
                    for upload_id, store_original in await pending_events(engine):
                        queue = STORE_QUEUE if store_original else THUMBNAIL_QUEUE
                        await broker.publish(
                            str(upload_id), queue, persist=True, timeout=5
                        )
                        await mark_published(engine, upload_id, store_original=store_original)
                    await asyncio.sleep(0.5)
        except Exception as exc:
            logger.warning("Image event publication will retry: %s", type(exc).__name__)
            await asyncio.sleep(1)
