import sqlalchemy as sa
from alembic import op

revision = "0004_queue_original_uploads"
down_revision = "0003_add_image_thumbnails"
branch_labels = None
depends_on = None


def upgrade() -> None:
    '''Добавляем промежуточные байты и общий флаг публикации для двух очередей.'''
    op.add_column("images", sa.Column("staged_content", sa.LargeBinary()))
    op.add_column(
        "images", sa.Column("thumbnail_failed", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    op.alter_column("images", "content", existing_type=sa.LargeBinary(), nullable=True)
    op.create_check_constraint(
        "images_staged_content_size", "images", "octet_length(staged_content) BETWEEN 1 AND 10485760"
    )
    op.create_check_constraint(
        "images_one_content", "images", "num_nonnulls(content, staged_content) = 1"
    )
    op.alter_column("images", "thumbnail_pending", new_column_name="event_pending")
    op.execute("ALTER INDEX images_thumbnail_pending_idx RENAME TO images_event_pending_idx")


def downgrade() -> None:
    '''Сохраняем принятые байты как оригиналы перед возвратом к синхронному контракту.'''
    op.execute(
        "UPDATE images SET content = staged_content, staged_content = NULL, event_pending = true "
        "WHERE staged_content IS NOT NULL"
    )
    op.execute("UPDATE images SET upload_id = NULL WHERE thumbnail_failed")
    op.execute("ALTER INDEX images_event_pending_idx RENAME TO images_thumbnail_pending_idx")
    op.alter_column("images", "event_pending", new_column_name="thumbnail_pending")
    op.alter_column("images", "content", existing_type=sa.LargeBinary(), nullable=False)
    op.drop_constraint("images_one_content", "images", type_="check")
    op.drop_constraint("images_staged_content_size", "images", type_="check")
    op.drop_column("images", "thumbnail_failed")
    op.drop_column("images", "staged_content")
