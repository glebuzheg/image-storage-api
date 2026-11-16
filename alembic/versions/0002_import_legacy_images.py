import os
from pathlib import Path
import re

from alembic import context, op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

revision = "0002_import_legacy_images"
down_revision = "0001_create_images"
branch_labels = None
depends_on = None

IMAGE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\.(jpg|jpeg|png)")
MAX_BYTES = 10 * 1024 * 1024
images = sa.table("images", sa.column("name", sa.Text), sa.column("content", sa.LargeBinary))


def upgrade() -> None:
    '''Импортируем файлы из прежнего тома и сохраняем его содержимое.'''
    if context.is_offline_mode():
        raise RuntimeError("Legacy image import requires online mode")
    directory = os.environ.get("LEGACY_IMAGE_DIR")
    if directory is None:
        return
    source = Path(directory)
    if not directory or not source.is_dir():
        raise RuntimeError("LEGACY_IMAGE_DIR must point to the mounted legacy directory")

    connection = op.get_bind()
    for path in source.iterdir():
        if not IMAGE_NAME.fullmatch(path.name) or path.is_symlink() or not path.is_file():
            continue
        with path.open("rb") as image:
            content = image.read(MAX_BYTES + 1)
        if not 1 <= len(content) <= MAX_BYTES:
            raise ValueError(f"Legacy image has an invalid size: {path.name}")
        connection.execute(
            insert(images).values(name=path.name, content=content)
            .on_conflict_do_nothing(index_elements=[images.c.name])
        )


def downgrade() -> None:
    '''Блокируем повторный импорт удалённых изображений при откате.'''
    raise RuntimeError("Legacy image import cannot be downgraded")
