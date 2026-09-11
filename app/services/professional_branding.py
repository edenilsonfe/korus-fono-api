"""Professional document branding: logo and signature used on exported documents."""

from __future__ import annotations

import logging

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.professional import Professional
from app.services.attachment_upload import normalize_content_type, sniff_content_type
from app.services.report_export import DocumentIdentity
from app.services.storage import storage_service

logger = logging.getLogger(__name__)

BRANDING_ASSET_FIELDS = {
    "logo": "branding_logo_key",
    "signature": "branding_signature_key",
}

BRANDING_ASSET_LABELS = {
    "logo": "Logotipo",
    "signature": "Assinatura",
}

# reportlab and python-docx embed PNG/JPEG natively; WebP would need Pillow.
_BRANDING_EXTENSIONS = {
    "image/png": "png",
    "image/jpeg": "jpg",
}

MAX_BRANDING_BYTES = 2 * 1024 * 1024


def branding_key(professional_id, asset: str, extension: str) -> str:
    return f"professionals/{professional_id}/branding/{asset}.{extension}"


def validate_branding_asset(asset: str) -> str:
    if asset not in BRANDING_ASSET_FIELDS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Recurso de identidade inválido.",
        )
    return asset


def validate_branding_image(*, content_type: str | None, body: bytes) -> str:
    """Return the normalized content type, or raise a pt-BR HTTPException."""
    if not body:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Arquivo vazio não é permitido.",
        )
    declared = normalize_content_type(content_type)
    if declared not in _BRANDING_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Formato de imagem não suportado. Use PNG ou JPEG.",
        )
    if len(body) > MAX_BRANDING_BYTES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Imagem maior que 2 MB.",
        )
    sniffed = sniff_content_type(body)
    if sniffed != declared:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Conteúdo do arquivo não corresponde ao tipo declarado.",
        )
    return declared


async def store_branding_image(
    db: AsyncSession,
    professional: Professional,
    asset: str,
    *,
    content_type: str | None,
    body: bytes,
) -> None:
    validate_branding_asset(asset)
    declared = validate_branding_image(content_type=content_type, body=body)
    key = branding_key(professional.id, asset, _BRANDING_EXTENSIONS[declared])
    await storage_service.upload(key, body, declared)
    # A re-upload with a different extension leaves the previous object behind;
    # the stored key always points at the newest file.
    setattr(professional, BRANDING_ASSET_FIELDS[asset], key)
    await db.flush()


async def delete_branding_image(db: AsyncSession, professional: Professional, asset: str) -> None:
    validate_branding_asset(asset)
    setattr(professional, BRANDING_ASSET_FIELDS[asset], None)
    await db.flush()


async def _presigned_asset_url(key: str | None) -> str | None:
    if not key:
        return None
    try:
        return await storage_service.presigned_url(key, as_attachment=False)
    except Exception:  # noqa: BLE001 - unavailable storage must not break settings
        logger.warning("Failed to presign branding asset", exc_info=True)
        return None


async def branding_urls(professional: Professional) -> tuple[str | None, str | None]:
    """Return (logo_url, signature_url) as inline presigned URLs."""
    logo_url = await _presigned_asset_url(professional.branding_logo_key)
    signature_url = await _presigned_asset_url(professional.branding_signature_key)
    return logo_url, signature_url


async def _download_asset_bytes(key: str | None) -> bytes | None:
    if not key:
        return None
    try:
        body, _content_type = await storage_service.download(key)
        return body or None
    except Exception:  # noqa: BLE001 - export must degrade to a document without images
        logger.warning("Failed to download branding asset for document", exc_info=True)
        return None


async def build_document_identity(professional: Professional) -> DocumentIdentity:
    """Resolve everything a document needs to carry the professional identity."""
    return DocumentIdentity(
        professional_name=(professional.name or "").strip(),
        council=(professional.council or "").strip(),
        logo_bytes=await _download_asset_bytes(professional.branding_logo_key),
        signature_bytes=await _download_asset_bytes(professional.branding_signature_key),
    )
