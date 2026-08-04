import re
import uuid
from contextlib import asynccontextmanager
from typing import Any

import aioboto3
from botocore.config import Config

from app.core.config import Settings, get_settings

_SAFE_FILENAME_RE = re.compile(r"[^\w.\- ()\[\]]+", re.UNICODE)
DEFAULT_PRESIGN_EXPIRES = 600


def s3_client_kwargs(settings: Settings) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "aws_access_key_id": settings.s3_access_key,
        "aws_secret_access_key": settings.s3_secret_key,
        "region_name": settings.s3_region,
        "config": Config(signature_version="s3v4"),
    }
    endpoint = settings.s3_endpoint_url
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    return kwargs


def safe_content_disposition_filename(key: str, filename: str | None = None) -> str:
    raw = (filename or key).replace("\\", "/")
    base = raw.rsplit("/", 1)[-1].strip().strip(".")
    if not base:
        base = "download"
    base = re.sub(r"[\x00-\x1f\x7f]", "", base)
    base = base.replace('"', "")
    base = _SAFE_FILENAME_RE.sub("_", base).strip("._") or "download"
    return base[:180]


class StorageService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self._session = aioboto3.Session()

    @asynccontextmanager
    async def _client(self):
        async with self._session.client("s3", **s3_client_kwargs(self.settings)) as client:
            yield client

    async def ensure_bucket(self) -> None:
        async with self._client() as client:
            try:
                await client.head_bucket(Bucket=self.settings.s3_bucket)
            except Exception:
                await client.create_bucket(Bucket=self.settings.s3_bucket)

    async def upload(self, key: str, body: bytes, content_type: str) -> str:
        await self.ensure_bucket()
        async with self._client() as client:
            await client.put_object(
                Bucket=self.settings.s3_bucket,
                Key=key,
                Body=body,
                ContentType=content_type,
            )
        return key

    async def presigned_url(
        self,
        key: str,
        expires: int = DEFAULT_PRESIGN_EXPIRES,
        *,
        filename: str | None = None,
        as_attachment: bool = True,
    ) -> str:
        safe = safe_content_disposition_filename(key, filename)
        disposition = "attachment" if as_attachment else "inline"
        params = {
            "Bucket": self.settings.s3_bucket,
            "Key": key,
            "ResponseContentDisposition": f'{disposition}; filename="{safe}"',
        }
        async with self._client() as client:
            return await client.generate_presigned_url(
                "get_object",
                Params=params,
                ExpiresIn=expires,
            )

    async def download(self, key: str) -> tuple[bytes, str | None]:
        """Fetch object bytes and its stored content type (None when absent)."""
        async with self._client() as client:
            obj = await client.get_object(Bucket=self.settings.s3_bucket, Key=key)
            body = await obj["Body"].read()
            return body, obj.get("ContentType")

    @staticmethod
    def make_key(patient_id: uuid.UUID, filename: str) -> str:
        return f"patients/{patient_id}/{uuid.uuid4()}/{filename}"

    @staticmethod
    def make_resource_key(resource_id: uuid.UUID, filename: str) -> str:
        return f"resources/{resource_id}/{filename}"


storage_service = StorageService()
