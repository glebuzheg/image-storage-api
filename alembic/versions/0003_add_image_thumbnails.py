import sqlalchemy as sa
from alembic import op

revision = "0003_add_image_thumbnails"
down_revision = "0002_import_legacy_images"
branch_labels = None
depends_on = None


def upgrade() -> None:
    '''Добавляем миниатюры и очередь публикации к существующим строкам.'''
    op.add_column("images", sa.Column("upload_id", sa.Uuid()))
    op.add_column("images", sa.Column("thumbnail", sa.LargeBinary()))
    op.add_column(
        "images", sa.Column("thumbnail_pending", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    op.create_unique_constraint("images_upload_id_key", "images", ["upload_id"])
    op.create_index(
        "images_thumbnail_pending_idx",
        "images",
        ["upload_id"],
        postgresql_where=sa.text("thumbnail_pending"),
    )


def downgrade() -> None:
    '''Удаляем задания и миниатюры, сохраняя исходные изображения.'''
    op.drop_index("images_thumbnail_pending_idx", table_name="images")
    op.drop_constraint("images_upload_id_key", "images", type_="unique")
    op.drop_column("images", "thumbnail_pending")
    op.drop_column("images", "thumbnail")
    op.drop_column("images", "upload_id")
