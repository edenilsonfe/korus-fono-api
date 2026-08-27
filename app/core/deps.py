from uuid import UUID

import sentry_sdk
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_cookies import ACCESS_COOKIE
from app.core.admin_permissions import (
    PERMISSION_ADMIN_ACCESS,
    has_admin_permission,
    permissions_for_role,
    resolve_admin_role,
)
from app.core.security import decode_token
from app.db.session import get_db
from app.models.patient import Patient
from app.models.professional import Professional

bearer_scheme = HTTPBearer(auto_error=False)
PAYMENT_REQUIRED_DETAIL = (
    "Pagamento pendente. Conclua a assinatura para acessar o KorusFono."
)


def _bind_sentry_user(professional: Professional) -> None:
    # LGPD: only opaque id — never email/CPF/phone
    sentry_sdk.set_user({"id": str(professional.id)})


def _assert_token_version(payload: dict, professional: Professional) -> None:
    token_version = int(payload.get("tv", 0))
    if token_version != professional.token_version:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sessão invalidada. Faça login novamente.",
        )


def _extract_access_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None,
) -> str | None:
    if credentials is not None:
        return credentials.credentials
    cookie_token = request.cookies.get(ACCESS_COOKIE, "").strip()
    return cookie_token or None


async def get_current_professional(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> Professional:
    token = _extract_access_token(request, credentials)
    if token is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Não autenticado")
    try:
        payload = decode_token(token)
        if payload.get("type") != "access":
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token inválido")
        professional_id = UUID(payload["sub"])
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token inválido") from exc

    result = await db.execute(select(Professional).where(Professional.id == professional_id))
    professional = result.scalar_one_or_none()
    if professional is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Profissional não encontrado")
    if professional.is_disabled:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Conta desativada")
    _assert_token_version(payload, professional)
    _bind_sentry_user(professional)
    return professional


async def get_optional_professional(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> Professional | None:
    token = _extract_access_token(request, credentials)
    if token is None:
        return None
    try:
        payload = decode_token(token)
        if payload.get("type") != "access":
            return None
        professional_id = UUID(payload["sub"])
    except Exception:
        return None

    result = await db.execute(select(Professional).where(Professional.id == professional_id))
    professional = result.scalar_one_or_none()
    if professional is None or professional.is_disabled:
        return None
    try:
        _assert_token_version(payload, professional)
    except HTTPException:
        return None
    _bind_sentry_user(professional)
    return professional


async def require_verified_professional(
    professional: Professional = Depends(get_current_professional),
) -> Professional:
    if professional.signup_payment_required:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=PAYMENT_REQUIRED_DETAIL,
        )
    if professional.email_verified_at is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="E-mail não verificado",
        )
    return professional


async def require_staff(
    professional: Professional = Depends(require_verified_professional),
) -> Professional:
    """Compatibility gate for any platform administrator."""
    role = resolve_admin_role(
        admin_role=professional.admin_role,
        is_staff=professional.is_staff,
    )
    if not has_admin_permission(role, PERMISSION_ADMIN_ACCESS):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acesso restrito a administradores da plataforma",
        )
    return professional


def require_admin_permission(permission: str):
    async def dependency(
        professional: Professional = Depends(require_verified_professional),
    ) -> Professional:
        role = resolve_admin_role(
            admin_role=professional.admin_role,
            is_staff=professional.is_staff,
        )
        if not has_admin_permission(role, permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Seu papel administrativo não permite esta ação",
            )
        return professional

    return dependency


def admin_permissions_for(professional: Professional) -> list[str]:
    role = resolve_admin_role(
        admin_role=professional.admin_role,
        is_staff=professional.is_staff,
    )
    return sorted(permissions_for_role(role))


async def get_patient_for_professional(
    patient_id: UUID,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
) -> Patient:
    result = await db.execute(
        select(Patient).where(Patient.id == patient_id, Patient.professional_id == professional.id)
    )
    patient = result.scalar_one_or_none()
    if patient is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Paciente não encontrado")
    return patient
