import os
import struct
from collections.abc import AsyncIterator
from io import BytesIO
from tempfile import TemporaryFile
from typing import BinaryIO

from anyio import CancelScope, CapacityLimiter
from anyio.to_thread import run_sync
from PIL import Image

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 25_000_000
PNG_END = b"\x00\x00\x00\x00IEND\xaeB\x60\x82"
validation_slots = CapacityLimiter(2)


class ImageTooLarge(Exception):
    pass


class InvalidImage(Exception):
    pass


async def read_image(name: str, chunks: AsyncIterator[bytes]) -> bytes:
    '''Принимаем поток с лимитом размера и возвращаем проверенные байты.'''
    temporary = await run_sync(TemporaryFile)
    try:
        size = 0
        async for chunk in chunks:
            size += len(chunk)
            if size > MAX_IMAGE_BYTES:
                raise ImageTooLarge
            if chunk:
                await run_sync(temporary.write, chunk)
        return await run_sync(_validate_and_read, temporary, name, limiter=validation_slots)
    finally:
        with CancelScope(shield=True):
            await run_sync(temporary.close)


def _validate_and_read(temporary: BinaryIO, name: str) -> bytes:
    '''Декодируем изображение для проверки и считываем исходные байты.'''
    temporary.flush()
    image_format = "PNG" if name.endswith(".png") else "JPEG"
    try:
        with Image.open(temporary, formats=[image_format]) as image:
            frame_count = getattr(image, "n_frames", 1)
            if image.width * image.height * frame_count > MAX_IMAGE_PIXELS:
                raise ImageTooLarge
            image.verify()
            if image_format == "PNG":
                # Pillow stops at the IEND header before checking its checksum.
                temporary.seek(-8, os.SEEK_CUR)
                if temporary.read(len(PNG_END) + 1) != PNG_END:
                    raise InvalidImage
        with Image.open(temporary, formats=[image_format]) as image:
            for frame in range(frame_count):
                image.seek(frame)
                image.load()
    except Image.DecompressionBombError as exc:
        raise ImageTooLarge from exc
    except OSError as exc:
        if exc.errno is not None:
            raise
        raise InvalidImage from exc
    except (EOFError, IndexError, SyntaxError, ValueError, struct.error) as exc:
        raise InvalidImage from exc

    temporary.seek(0)
    return temporary.read()


def render_thumbnail(content: bytes) -> bytes:
    '''Уменьшаем первый кадр до PNG с максимальной стороной 256 пикселей.'''
    try:
        with BytesIO(content) as source, Image.open(source, formats=["JPEG", "PNG"]) as image:
            if getattr(image, "is_animated", False) and image.info.get("default_image") is True:
                image.seek(1)
            image.load()
            image.info.clear()
            with image.convert("RGBA") as thumbnail, BytesIO() as output:
                thumbnail.thumbnail((256, 256), Image.Resampling.LANCZOS)
                thumbnail.save(output, format="PNG")
                return output.getvalue()
    except OSError as exc:
        if exc.errno is not None:
            raise
        raise InvalidImage from exc
    except (
        Image.DecompressionBombError, EOFError, IndexError, SyntaxError, TypeError,
        ValueError, struct.error,
    ) as exc:
        raise InvalidImage from exc
