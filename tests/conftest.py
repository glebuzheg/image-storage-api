import os
from contextlib import contextmanager
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, make_url
from sqlalchemy.schema import CreateSchema, DropSchema


@pytest.fixture(scope="session")
def postgres_base_url():
    '''Получаем адрес выделенного тестового PostgreSQL.'''
    configured_url = os.environ.get("TEST_DATABASE_URL")
    if not configured_url:
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL integration tests")
    return make_url(configured_url)


@contextmanager
def isolated_schema(base_url):
    '''Создаём и удаляем схему со случайным именем.'''
    schema = f"image_test_{uuid4().hex}"
    schema_url = base_url.update_query_dict({"options": f"-csearch_path={schema}"})
    engine = create_engine(base_url)
    try:
        with engine.begin() as connection:
            connection.execute(CreateSchema(schema))
        yield schema_url.render_as_string(hide_password=False)
    finally:
        try:
            with engine.begin() as connection:
                connection.execute(DropSchema(schema, cascade=True, if_exists=True))
        finally:
            engine.dispose()


@pytest.fixture(scope="session")
def test_database_url(postgres_base_url):
    '''Подготавливаем проверенную миграциями таблицу для тестов маршрутов.'''
    with isolated_schema(postgres_base_url) as url:
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv("DATABASE_URL", url)
            command.upgrade(Config("alembic.ini"), "head")
        yield url
