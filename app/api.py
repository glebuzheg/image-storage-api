import re
from hashlib import sha256
from hmac import compare_digest
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from starlette.requests import ClientDisconnect

from app.db import delete_image as delete_image_record
from app.db import get_image, get_thumbnail, get_upload_status, stage_upload
from app.storage import MAX_IMAGE_BYTES, ImageTooLarge, InvalidImage, read_image

IMAGE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\.(jpg|jpeg|png)")


async def require_key(request: Request) -> None:
    '''Сравниваем хеш полученного ключа с настроенным хешем до доступа к файлам.'''
    supplied_key = request.headers.get("x-api-key", "").encode("latin-1")
    supplied_hash = sha256(supplied_key).digest()
    if not supplied_key or not compare_digest(supplied_hash, request.app.state.api_key_hash):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


router = APIRouter(prefix="/images", dependencies=[Depends(require_key)])
uploads_router = APIRouter(prefix="/uploads", dependencies=[Depends(require_key)])


class UploadStatus(BaseModel):
    status: Literal["pending", "stored"]
    image_url: str


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


@router.put("/{name}", status_code=202, response_class=Response)
async def upload_image(
    name: Annotated[str, Depends(check_upload)], request: Request
) -> Response:
    '''Подтверждаем надёжный приём байтов и задания на запись.'''
    try:
        content = await read_image(name, request.stream())
    except ImageTooLarge:
        raise HTTPException(status_code=413, detail="Image exceeds the byte or pixel limit") from None
    except InvalidImage:
        raise HTTPException(status_code=415, detail="Invalid image or format mismatch") from None
    except ClientDisconnect:
        raise HTTPException(status_code=400, detail="Image upload interrupted") from None

    upload_id = await stage_upload(request.app.state.db, name, content)
    if upload_id is None:
        raise HTTPException(status_code=409, detail="Image name already exists")
    return Response(status_code=202, headers={"Location": f"/uploads/{upload_id}"})


@uploads_router.get("/{upload_id}")
async def upload_status(upload_id: UUID, request: Request, response: Response) -> UploadStatus:
    '''Показываем, записан ли оригинал принятой загрузки.'''
    result = await get_upload_status(request.app.state.db, upload_id)
    if result is None:
        raise HTTPException(
            status_code=404, detail="Upload not found", headers={"Cache-Control": "no-store"}
        )
    name, stored = result
    response.headers["Cache-Control"] = "no-store"
    if not stored:
        response.headers["Retry-After"] = "1"
    return UploadStatus(status="stored" if stored else "pending", image_url=f"/images/{name}")


@router.get("/{name}")
async def download_image(name: ImageName, request: Request) -> Response:
    '''Считываем байты из базы и отдаём их как вложение.'''
    content = await get_image(request.app.state.db, name)
    if content is None:
        raise HTTPException(
            status_code=404, detail="Image not found", headers={"Cache-Control": "no-store"}
        )
    media_type = "image/png" if name.endswith(".png") else "image/jpeg"
    return Response(
        content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.get(
    "/{name}/thumbnail",
    response_class=Response,
    responses={200: {"content": {"image/png": {}}}},
)
async def download_thumbnail(name: ImageName, request: Request) -> Response:
    '''Возвращаем готовый PNG или статус отсутствующей миниатюры.'''
    headers = {"Cache-Control": "no-store"}
    result = await get_thumbnail(request.app.state.db, name)
    thumbnail, pending = result if result is not None else (None, False)
    if thumbnail is not None:
        return Response(thumbnail, media_type="image/png", headers=headers)
    if pending:
        headers["Retry-After"] = "1"
        raise HTTPException(status_code=404, detail="Thumbnail pending", headers=headers)
    raise HTTPException(status_code=404, detail="Thumbnail not found", headers=headers)


@router.delete("/{name}", status_code=204)
async def delete_image(name: ImageName, request: Request) -> Response:
    '''Удаляем запись из PostgreSQL и освобождаем имя.'''
    if not await delete_image_record(request.app.state.db, name):
        raise HTTPException(status_code=404, detail="Image not found")
    return Response(status_code=204)
