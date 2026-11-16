import re
from hashlib import sha256
from hmac import compare_digest
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from starlette.requests import ClientDisconnect

from app.db import delete_image as delete_image_record
from app.db import get_image, insert_image
from app.storage import MAX_IMAGE_BYTES, ImageTooLarge, InvalidImage, read_image

IMAGE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\.(jpg|jpeg|png)")


async def require_key(request: Request) -> None:
    '''Сравниваем хеш полученного ключа с настроенным хешем до доступа к файлам.'''
    supplied_key = request.headers.get("x-api-key", "").encode("latin-1")
    supplied_hash = sha256(supplied_key).digest()
    if not supplied_key or not compare_digest(supplied_hash, request.app.state.api_key_hash):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


router = APIRouter(prefix="/images", dependencies=[Depends(require_key)])


async def check_name(name: str) -> str:
    '''Пропускаем только имена, которые можно безопасно использовать внутри каталога.'''
    if not IMAGE_NAME.fullmatch(name):
        raise HTTPException(status_code=400, detail="Invalid image name")
    return name


ImageName = Annotated[str, Depends(check_name)]


async def check_upload(name: ImageName, request: Request) -> str:
    '''Проверяем заявленный размер до чтения тела и возвращаем проверенное имя.'''
    try:
        declared_size = int(request.headers.get("content-length", "0"))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid Content-Length") from None
    if declared_size < 0:
        raise HTTPException(status_code=400, detail="Invalid Content-Length")
    if declared_size > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="Image exceeds the byte limit")
    return name


@router.put("/{name}", status_code=201)
async def upload_image(
    name: Annotated[str, Depends(check_upload)], request: Request
) -> Response:
    '''Подтверждаем загрузку после проверки и публикации файла.'''
    try:
        content = await read_image(name, request.stream())
    except ImageTooLarge:
        raise HTTPException(status_code=413, detail="Image exceeds the byte or pixel limit") from None
    except InvalidImage:
        raise HTTPException(status_code=415, detail="Invalid image or format mismatch") from None
    except ClientDisconnect:
        raise HTTPException(status_code=400, detail="Image upload interrupted") from None

    if not await insert_image(request.app.state.db, name, content):
        raise HTTPException(status_code=409, detail="Image name already exists")
    return Response(status_code=201, headers={"Location": f"/images/{name}"})


@router.get("/{name}")
async def download_image(name: ImageName, request: Request) -> Response:
    '''Считываем байты из базы и отдаём их как вложение.'''
    content = await get_image(request.app.state.db, name)
    if content is None:
        raise HTTPException(status_code=404, detail="Image not found")
    media_type = "image/png" if name.endswith(".png") else "image/jpeg"
    return Response(
        content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.delete("/{name}", status_code=204)
async def delete_image(name: ImageName, request: Request) -> Response:
    '''Удаляем запись из PostgreSQL и освобождаем имя.'''
    if not await delete_image_record(request.app.state.db, name):
        raise HTTPException(status_code=404, detail="Image not found")
    return Response(status_code=204)
