import asyncio
import inspect
import re
import uuid
from contextlib import asynccontextmanager
from typing import Any

import aioboto3
from botocore.config import Config

from app.core.config import Settings, get_settings

_SAFE_FILENAME_RE = re.compile(r"[^\w.\- ()\[\]]+", re.UNICODE)
DEFAULT_PRESIGN_EXPIRES = 600
# F6 — downloads com orçamento de bytes leem em blocos para medir o corpo real.
DOWNLOAD_CHUNK_SIZE = 1024 * 1024
DEFAULT_DOWNLOAD_LIMIT_TIMEOUT = 30.0
_MISSING_OBJECT_CODES = {"NoSuchKey", "NoSuchBucket", "NotFound", "404"}


def is_missing_object_error(exc: BaseException) -> bool:
    """Erro do S3/``FileNotFoundError`` que significa "objeto já não existe"."""
    if isinstance(exc, FileNotFoundError):
        return True
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error") or {}
        if str(error.get("Code", "")) in _MISSING_OBJECT_CODES:
            return True
        http_code = str((response.get("ResponseMetadata") or {}).get("HTTPStatusCode", ""))
        return http_code == "404"
    return False


def validate_deletable_object_key(key: str) -> None:
    """Bloqueia chave vazia/prefixo: só objeto único pode ser excluído."""
    if not key or not key.strip():
        raise ValueError("Chave de objeto obrigatória para exclusão.")
    parts = key.split("/")
    if key.startswith("/") or key.endswith("/") or any(part in ("", ".", "..") for part in parts):
        raise ValueError("Chave de objeto inválida para exclusão (prefixo/bucket não são removíveis).")


class StorageLimitExceededError(RuntimeError):
    """Objeto maior que o orçamento de bytes informado pelo chamador.

    Levantada por ``StorageService.download_limited`` assim que o tamanho real
    (ou o ``ContentLength`` declarado) cruza ``max_bytes`` — nunca devolve um
    corpo truncado como se fosse completo.
    """


async def _close_body(body: Any) -> None:
    """Fecha o stream do objeto (sync ou async), sem propagar erro de cleanup."""
    if body is None:
        return
    close = getattr(body, "close", None) or getattr(body, "aclose", None)
    if close is None:
        return
    try:
        result = close()
        if inspect.isawaitable(result):
            await result
    except Exception:  # noqa: BLE001 - cleanup não pode mascarar o erro original
        pass


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

    async def delete(self, key: str) -> None:
        """Remove UM objeto exato; objeto ausente conta como SUCESSO.

        Nunca remove prefixo/bucket nem opera em lote — apenas ``delete_object``
        com a chave exata (chaves são imutáveis por operação, então repetir a
        remoção é seguro). Outras falhas sobem para o chamador decidir
        (retentativa/backoff no janitor).
        """
        validate_deletable_object_key(key)
        async with self._client() as client:
            try:
                await client.delete_object(Bucket=self.settings.s3_bucket, Key=key)
            except Exception as exc:  # noqa: BLE001 — ausência é sucesso; o resto sobe
                if not is_missing_object_error(exc):
                    raise

    async def download_limited(
        self,
        key: str,
        max_bytes: int,
        timeout_seconds: float = DEFAULT_DOWNLOAD_LIMIT_TIMEOUT,
    ) -> tuple[bytes, str | None]:
        """Fetch object bytes enforcing a real byte budget and a deadline.

        ``ContentLength`` is only an early check: the body is read in bounded
        chunks and measured as it arrives, so a wrong (or absent) header cannot
        smuggle a larger object. The stream is always closed and
        ``StorageLimitExceededError`` is raised the moment the real total
        crosses ``max_bytes`` — never a silently truncated body.
        """
        if max_bytes <= 0:
            raise ValueError("max_bytes deve ser positivo")

        async with self._client() as client:
            obj = await asyncio.wait_for(
                client.get_object(Bucket=self.settings.s3_bucket, Key=key),
                timeout=timeout_seconds,
            )
            body = obj.get("Body")
            try:
                declared = obj.get("ContentLength")
                if isinstance(declared, int) and declared > max_bytes:
                    raise StorageLimitExceededError(
                        f"Objeto excede o orçamento de {max_bytes} bytes"
                    )
                payload = await asyncio.wait_for(
                    self._read_body_limited(body, max_bytes), timeout=timeout_seconds
                )
                return payload, obj.get("ContentType")
            finally:
                await _close_body(body)

    @staticmethod
    async def _read_body_limited(body: Any, max_bytes: int) -> bytes:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = await body.read(DOWNLOAD_CHUNK_SIZE)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise StorageLimitExceededError(
                    f"Objeto excede o orçamento de {max_bytes} bytes"
                )
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    def make_key(patient_id: uuid.UUID, filename: str) -> str:
        return f"patients/{patient_id}/{uuid.uuid4()}/{filename}"

    @staticmethod
    def make_resource_key(resource_id: uuid.UUID, filename: str) -> str:
        """Chave imutável por operação dentro de ``resources/{id}/...``.

        Cada upload/substituição usa um id de operação novo; a chave antiga
        continua válida porque a chave efetiva fica persistida no recurso
        (compatibilidade das chaves legadas sem o id de operação).
        """
        return f"resources/{resource_id}/{uuid.uuid4()}/{filename}"


storage_service = StorageService()
