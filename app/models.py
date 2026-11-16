from sqlalchemy import CheckConstraint, LargeBinary, Text
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
    )

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
