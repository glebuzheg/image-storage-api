from uuid import UUID

from sqlalchemy import CheckConstraint, Index, LargeBinary, Text, UniqueConstraint, Uuid, false, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class StoredImage(Base):
    __tablename__ = "images"
    __table_args__ = (
        CheckConstraint(
            "octet_length(content) BETWEEN 1 AND 10485760",
            name="images_content_size",
        ),
        CheckConstraint(
            "octet_length(staged_content) BETWEEN 1 AND 10485760",
            name="images_staged_content_size",
        ),
        CheckConstraint("num_nonnulls(content, staged_content) = 1", name="images_one_content"),
        UniqueConstraint("upload_id", name="images_upload_id_key"),
        Index("images_event_pending_idx", "upload_id", postgresql_where=text("event_pending")),
    )

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    content: Mapped[bytes | None] = mapped_column(LargeBinary)
    staged_content: Mapped[bytes | None] = mapped_column(LargeBinary)
    upload_id: Mapped[UUID | None] = mapped_column(Uuid)
    thumbnail: Mapped[bytes | None] = mapped_column(LargeBinary)
    thumbnail_failed: Mapped[bool] = mapped_column(server_default=false())
    event_pending: Mapped[bool] = mapped_column(server_default=false())
