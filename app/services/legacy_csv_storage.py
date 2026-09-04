"""Conditional private-object writes for the CSV migration's recoverable uploads."""

from __future__ import annotations

import aioboto3
from botocore.exceptions import ClientError

from app.core.config import Settings, get_settings
from app.services.legacy_csv_source import CsvImportError, digest
from app.services.storage import s3_client_kwargs


class CsvImportStorage:
    def __init__(self, *, settings: Settings | None = None):
        self.settings = settings if settings is not None else get_settings()
        self.session = aioboto3.Session()

    async def check(self) -> None:
        async with self.session.client(
            "s3", **s3_client_kwargs(self.settings)
        ) as client:
            await client.head_bucket(Bucket=self.settings.s3_bucket)

    async def read(self, key: str) -> bytes | None:
        async with self.session.client(
            "s3", **s3_client_kwargs(self.settings)
        ) as client:
            try:
                obj = await client.get_object(Bucket=self.settings.s3_bucket, Key=key)
            except ClientError as exc:
                if exc.response["Error"]["Code"] in {"NoSuchKey", "404"}:
                    return None
                raise
            async with obj["Body"] as body:
                return await body.read()

    async def put_verified(self, key: str, payload: bytes) -> None:
        """Never overwrite an existing object, even during concurrent retries."""
        async with self.session.client(
            "s3", **s3_client_kwargs(self.settings)
        ) as client:
            extra = (
                {}
                if self.settings.s3_endpoint_url
                else {"ServerSideEncryption": "AES256"}
            )
            try:
                await client.put_object(
                    Bucket=self.settings.s3_bucket,
                    Key=key,
                    Body=payload,
                    ContentType="application/pdf",
                    Metadata={"sha256": digest(payload)},
                    IfNoneMatch="*",
                    **extra,
                )
            except ClientError as exc:
                if exc.response["Error"]["Code"] not in {"PreconditionFailed", "412"}:
                    raise
        actual = await self.read(key)
        if actual is None or digest(actual) != digest(payload):
            raise CsvImportError("Objeto do lote ausente ou divergente no storage")
