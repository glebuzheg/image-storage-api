from alembic import op
import sqlalchemy as sa

revision = "0001_create_images"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    '''Создаём таблицу изображений с уникальным именем и ограничением размера.'''
    op.create_table(
        "images",
        sa.Column("name", sa.Text(), primary_key=True),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.CheckConstraint(
            "octet_length(content) BETWEEN 1 AND 10485760",
            name="images_content_size",
        ),
    )


def downgrade() -> None:
    '''Удаляем таблицу изображений при откате схемы.'''
    op.drop_table("images")
