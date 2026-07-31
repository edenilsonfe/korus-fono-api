from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.core.deps import require_verified_professional
from app.core.instrument_aliases import (
    CLIENT_SCORED_PROTOCOLS,
    has_manifest_package,
    resolve_instrument_slug,
)
from app.models.professional import Professional
from app.schemas.instrument import (
    InstrumentContentResponse,
    InstrumentManifestResponse,
    InstrumentScoreRequest,
    InstrumentScoreResponse,
    ProtocolCapabilitiesResponse,
)
from app.services.assessment_scoring import get_protocol_scoring_mode, score_manifest_protocol
from app.services.instrument_content_package import get_instrument_content_package
from app.services.instrument_scoring_service import InstrumentScoringService

router = APIRouter(prefix="/instruments", tags=["instruments"])


@router.get("/{protocol_id}/capabilities", response_model=ProtocolCapabilitiesResponse)
async def get_protocol_capabilities(
    protocol_id: str,
    _professional: Professional = Depends(require_verified_professional),
):
    pid = protocol_id.lower()
    mode = get_protocol_scoring_mode(pid)
    slug = resolve_instrument_slug(pid)
    has_items = False
    if slug and has_manifest_package(pid):
        try:
            package = get_instrument_content_package(slug)
            has_items = len(package.get_items()) > 0 or bool(package.modules)
        except FileNotFoundError:
            has_items = False
    elif pid in CLIENT_SCORED_PROTOCOLS:
        has_items = True
    return ProtocolCapabilitiesResponse(
        protocol_id=pid,
        scoring_mode=mode,
        instrument_slug=slug,
        has_items=has_items,
    )


@router.get("/{protocol_id}/manifest", response_model=InstrumentManifestResponse)
async def get_instrument_manifest(
    protocol_id: str,
    _professional: Professional = Depends(require_verified_professional),
):
    slug = resolve_instrument_slug(protocol_id)
    if not slug:
        raise HTTPException(status_code=404, detail="Protocolo sem pacote de instrumento")
    try:
        package = get_instrument_content_package(slug)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Pacote do instrumento não encontrado") from exc
    return InstrumentManifestResponse(**package.public_manifest())


@router.get("/{protocol_id}/content", response_model=InstrumentContentResponse)
async def get_instrument_content(
    protocol_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    section: str | None = None,
    module: str | None = None,
    _professional: Professional = Depends(require_verified_professional),
):
    slug = resolve_instrument_slug(protocol_id)
    if not slug:
        raise HTTPException(status_code=404, detail="Protocolo sem pacote de instrumento")
    try:
        package = get_instrument_content_package(slug)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Pacote do instrumento não encontrado") from exc

    payload = package.get_items_page(page=page, page_size=page_size, section=section, module=module)
    return InstrumentContentResponse(
        instrument_slug=slug,
        scale=package.scale,
        domains=package.domains,
        items=payload["items"],
        page=payload["page"],
        page_size=payload["page_size"],
        total_items=payload["total_items"],
        total_pages=payload["total_pages"],
    )


@router.post("/{protocol_id}/score", response_model=InstrumentScoreResponse)
async def score_instrument(
    protocol_id: str,
    data: InstrumentScoreRequest,
    _professional: Professional = Depends(require_verified_professional),
):
    if not has_manifest_package(protocol_id):
        raise HTTPException(status_code=400, detail="Este protocolo não usa scoring no servidor")
    try:
        scores = score_manifest_protocol(protocol_id, data.answers)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Pacote do instrumento não encontrado") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return InstrumentScoreResponse(**scores)
