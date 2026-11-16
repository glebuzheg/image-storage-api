from alembic import context
from sqlalchemy import create_engine, pool

from app.config import database_url
from app.models import Base


def run_migrations_offline() -> None:
    '''Генерируем SQL для схемы без подключения к базе.'''
    context.configure(url=database_url(), target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    '''Выполняем миграции в PostgreSQL до старта API.'''
    engine = create_engine(database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
