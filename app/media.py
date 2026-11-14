import hashlib
import io
import logging
import os
import uuid

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException
from PIL import Image, ImageOps, UnidentifiedImageError

Image.MAX_IMAGE_PIXELS = 25000000
BUCKET = os.getenv("S3_BUCKET", "fieldpulse")
logger = logging.getLogger("fieldpulse.media")


def client():
    return boto3.client(
        "s3", endpoint_url=os.getenv("S3_ENDPOINT", "http://minio:9000"),
        aws_access_key_id=os.getenv("S3_ACCESS_KEY", "fieldpulse"),
        aws_secret_access_key=os.getenv("S3_SECRET_KEY", "change-me"),
        region_name="us-east-1"
    )


def bucket_ready(c):
    try:
        c.head_bucket(Bucket=BUCKET)
    except ClientError as exc:
        if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
            c.create_bucket(Bucket=BUCKET)
        else:
            raise


def delete_image(image):
    """Best-effort compensation when the matching database transaction rolls back."""
    storage = image.get("storage_key")
    thumbnail = image.get("thumb_key")
    if not storage and not thumbnail:
        return True
    c = client()
    ok = True
    for key in (storage, thumbnail):
        if not key:
            continue
        try:
            c.delete_object(Bucket=BUCKET, Key=key)
        except (ClientError, BotoCoreError):
            ok = False
            logger.warning("Could not remove an uncommitted media object")
    return ok


def save_image(data: bytes, ticket_id: str):
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(413, detail="Файл превышает 10 МБ")
    try:
        with Image.open(io.BytesIO(data)) as source:
            if source.format not in ("JPEG", "PNG", "WEBP"):
                raise HTTPException(415, detail="Поддерживаются JPEG, PNG и WEBP")
            im = ImageOps.exif_transpose(source).convert("RGB")
            original = io.BytesIO()
            im.save(original, format="JPEG", quality=88)
            thumb = im.copy()
            thumb.thumbnail((360, 360))
            reduced = io.BytesIO()
            thumb.save(reduced, format="JPEG", quality=75)
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise HTTPException(415, detail="Некорректное изображение")

    key = "tickets/%s/%s.jpg" % (ticket_id, uuid.uuid4().hex)
    thumb_key = key.replace(".jpg", "-thumb.jpg")
    image = {
        "storage_key": key, "thumb_key": thumb_key,
        "checksum": hashlib.sha256(original.getvalue()).hexdigest(),
        "mime": "image/jpeg", "size": len(original.getvalue())
    }
    c = client()
    uploaded = []
    try:
        bucket_ready(c)
        c.put_object(Bucket=BUCKET, Key=key, Body=original.getvalue(),
                     ContentType="image/jpeg")
        uploaded.append(key)
        c.put_object(Bucket=BUCKET, Key=thumb_key, Body=reduced.getvalue(),
                     ContentType="image/jpeg")
        uploaded.append(thumb_key)
    except (ClientError, BotoCoreError):
        for uploaded_key in uploaded:
            try:
                c.delete_object(Bucket=BUCKET, Key=uploaded_key)
            except (ClientError, BotoCoreError):
                logger.warning("Partial image upload left an orphaned object")
        raise HTTPException(503, detail="Хранилище фотографий недоступно")
    return image


def read_image(key: str):
    try:
        return client().get_object(Bucket=BUCKET, Key=key)["Body"].read()
    except (ClientError, BotoCoreError):
        raise HTTPException(503, detail="Файл временно недоступен")
