from __future__ import annotations

import hashlib
import io
from functools import cached_property
from typing import Protocol

from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from PIL import Image, UnidentifiedImageError

from .errors import BusinessError

MAX_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 20_000_000
IMAGE_TYPES = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}


class ObjectStore(Protocol):
    def put(self, key: str, data: bytes, content_type: str) -> None: ...
    def get(self, key: str, expected_size: int) -> bytes: ...
    def presign(self, key: str, content_type: str) -> str: ...


def validate_image(data: bytes) -> tuple[str, str]:
    if len(data) > MAX_BYTES:
        raise BusinessError("MATERIAL_TOO_LARGE", "照片最大为 8 MiB。", 413)
    try:
        with Image.open(io.BytesIO(data)) as image:
            content_type = IMAGE_TYPES.get(image.format or "")
            if (
                content_type is None
                or image.width * image.height > MAX_PIXELS
                or getattr(image, "is_animated", False)
            ):
                raise ValueError("unsupported image")
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            image.load()
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError, UnidentifiedImageError):
        raise BusinessError(
            "INVALID_IMAGE", "请上传可解码的单张 JPEG、PNG 或 WebP 照片。", 422
        ) from None
    return content_type, hashlib.sha256(data).hexdigest()


def verify_object(data: bytes, expected_size: int, expected_hash: str) -> None:
    if len(data) != expected_size or hashlib.sha256(data).hexdigest() != expected_hash:
        raise BusinessError("MATERIAL_INTEGRITY_ERROR", "材料对象与登记记录不一致。", 503)


class S3ObjectStore:
    def __init__(self, settings) -> None:
        self.settings = settings

    @cached_property
    def client(self):
        return self._client(self.settings.object_endpoint)

    @cached_property
    def signing_client(self):
        endpoint = getattr(self.settings, "object_presign_endpoint", None)
        if endpoint is None or endpoint == self.settings.object_endpoint:
            return self.client
        return self._client(endpoint)

    def _client(self, endpoint: str):
        import boto3

        access_key = self.settings.object_access_key
        secret = self.settings.object_secret_key
        if not endpoint or not access_key or not secret or not self.settings.object_bucket:
            raise BusinessError("OBJECT_STORE_UNAVAILABLE", "私有材料存储尚未配置。", 503)
        return boto3.client(
            "s3",
            endpoint_url=str(endpoint),
            aws_access_key_id=access_key,
            aws_secret_access_key=secret.get_secret_value()
            if hasattr(secret, "get_secret_value")
            else secret,
            region_name="us-east-1",
            config=Config(
                signature_version="s3v4",
                connect_timeout=3,
                read_timeout=10,
                retries={"max_attempts": 1},
                s3={"addressing_style": "path"},
            ),
        )

    @property
    def bucket(self) -> str:
        return self.settings.object_bucket

    def ensure_bucket(self) -> None:
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except ClientError as error:
            if error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 404:
                raise BusinessError(
                    "OBJECT_STORE_UNAVAILABLE", "无法访问私有材料存储。", 503
                ) from None
            self.client.create_bucket(Bucket=self.bucket)

    def put(self, key: str, data: bytes, content_type: str) -> None:
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
                CacheControl="private, no-store",
            )
        except (BotoCoreError, ClientError):
            raise BusinessError(
                "OBJECT_STORE_UNAVAILABLE", "材料暂时无法写入，请重试。", 503
            ) from None

    def get(self, key: str, expected_size: int) -> bytes:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            body = response["Body"]
            try:
                return body.read(expected_size + 1)
            finally:
                body.close()
        except ClientError as error:
            if error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
                raise BusinessError(
                    "MATERIAL_OBJECT_MISSING", "登记材料的存储对象缺失。", 503
                ) from None
            raise BusinessError("OBJECT_STORE_UNAVAILABLE", "材料暂时无法读取。", 503) from None
        except BotoCoreError:
            raise BusinessError("OBJECT_STORE_UNAVAILABLE", "材料暂时无法读取。", 503) from None

    def presign(self, key: str, content_type: str) -> str:
        try:
            return self.signing_client.generate_presigned_url(
                "get_object",
                Params={
                    "Bucket": self.bucket,
                    "Key": key,
                    "ResponseContentType": content_type,
                    "ResponseCacheControl": "private, no-store",
                },
                ExpiresIn=60,
            )
        except (BotoCoreError, ClientError):
            raise BusinessError(
                "OBJECT_STORE_UNAVAILABLE", "暂时无法签发材料访问链接。", 503
            ) from None
