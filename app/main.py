import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from app.api import router, uploads_router
from app.broker import publish_pending
from app.config import load_settings, rabbitmq_url


@asynccontextmanager
async def lifespan(app: FastAPI):
    '''Управляем пулом PostgreSQL и фоновой публикацией заданий.'''
    api_key_hash, url = load_settings()
    engine = create_async_engine(url, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            await connection.execute(select(1))
        app.state.api_key_hash = api_key_hash
        app.state.db = engine
        publisher = asyncio.create_task(publish_pending(engine, rabbitmq_url()))
        try:
            yield
        finally:
            publisher.cancel()
            with suppress(asyncio.CancelledError):
                await publisher
    finally:
        await engine.dispose()


app = FastAPI(
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.include_router(router)
app.include_router(uploads_router)
