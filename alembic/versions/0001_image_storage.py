import sqlalchemy as sa
from alembic import op

revision = "0001_image_storage"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    '''Создаём таблицу оригиналов, миниатюр и ожидающих событий.'''
    op.create_table(
        "images",
        sa.Column("name", sa.Text(), primary_key=True),
        sa.Column("content", sa.LargeBinary()),
        sa.Column("staged_content", sa.LargeBinary()),
        sa.Column("upload_id", sa.Uuid()),
        sa.Column("thumbnail", sa.LargeBinary()),
        sa.Column("thumbnail_failed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("event_pending", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.CheckConstraint(
            "octet_length(content) BETWEEN 1 AND 10485760",
            name="images_content_size",
        ),
        sa.CheckConstraint(
            "octet_length(staged_content) BETWEEN 1 AND 10485760",
            name="images_staged_content_size",
        ),
        sa.CheckConstraint("num_nonnulls(content, staged_content) = 1", name="images_one_content"),
        sa.UniqueConstraint("upload_id", name="images_upload_id_key"),
    )
    op.create_index(
        "images_event_pending_idx",
        "images",
        ["upload_id"],
        postgresql_where=sa.text("event_pending"),
    )


def downgrade() -> None:
    '''Удаляем таблицу изображений при откате начальной схемы.'''
    op.drop_table("images")
