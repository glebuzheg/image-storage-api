from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from app.api import router
from app.config import load_settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    '''Открываем пул PostgreSQL при запуске и закрываем его при остановке.'''
    api_key_hash, url = load_settings()
    engine = create_async_engine(url, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            await connection.execute(select(1))
        app.state.api_key_hash = api_key_hash
        app.state.db = engine
        yield
    finally:
        await engine.dispose()


app = FastAPI(
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.include_router(router)
