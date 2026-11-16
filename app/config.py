import os
import re

from sqlalchemy import URL, make_url


def database_url() -> URL:
    '''Собираем адрес PostgreSQL с безопасным представлением пароля.'''
    if url := os.environ.get("DATABASE_URL"):
        return make_url(url)
    password = os.environ.get("POSTGRES_PASSWORD")
    if not password:
        raise RuntimeError("POSTGRES_PASSWORD or DATABASE_URL must be set")
    return URL.create(
        "postgresql+psycopg",
        username="image_storage",
        password=password,
        host="db",
        database="image_storage",
    )


def load_settings() -> tuple[bytes, URL]:
    '''Читаем хеш ключа и адрес базы при запуске.'''
    key_hash = os.environ.get("API_KEY_HASH", "")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", key_hash):
        raise RuntimeError("API_KEY_HASH must be a 64-character SHA-256 hex digest")
    return bytes.fromhex(key_hash), database_url()
