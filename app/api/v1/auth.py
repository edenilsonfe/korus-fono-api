from datetime import UTC, date, datetime, timedelta

from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_cookies import (
    REFRESH_COOKIE,
    clear_auth_cookies,
    set_auth_cookies,
)
from app.core.client_ip import get_client_ip
from app.core.config import get_settings
from app.core.demo_patient import (
    DEMO_AVATAR_COLOR,
    DEMO_PATIENT_NAME,
    demo_patient_birth_date,
)
from app.core.deps import get_current_professional
from app.core.security import (
    create_access_token,
    hash_password,
    verify_password,
)
from app.db.session import get_db
from app.core.specialty_catalog import specialty_label
from app.models.patient import Patient
from app.models.professional import Professional
from app.schemas.auth import (
    ChangePasswordRequest,
    ForgotPasswordRequest,
    LoginRequest,
    MessageResponse,
    RefreshRequest,
    RegisterRequest,
    ResetPasswordRequest,
    TokenResponse,
    VerifyEmailRequest,
)
from app.services.auth_rate_limit import (
    enforce_forgot_rate_limit,
    enforce_login_rate_limit,
    enforce_register_rate_limit,
    enforce_reset_rate_limit,
)
from app.services.email_verification import (
    request_email_verification,
    send_email_verification_email_sync,
    verify_email_with_token,
)
from app.services.password_reset import (
    GENERIC_FORGOT_MESSAGE,
    change_password,
    request_password_reset,
    reset_password_with_token,
    send_password_reset_email_sync,
)
from app.services.refresh_token_service import (
    create_refresh_session,
    revoke_all_refresh_sessions,
    revoke_refresh_session,
    rotate_refresh_session,
)
from app.services.meta_pixel_service import MetaPixelService
from app.services.new_account_notification import send_new_account_notification_sync

router = APIRouter(prefix="/auth", tags=["auth"])


def _request_ip(request: Request) -> str:
    return get_client_ip(request)


async def track_registration_events_task(
    professional_id: str,
    email: str,
    name: str,
    phone: str | None,
    client_ip: str | None,
    client_user_agent: str | None,
    fbp: str | None,
    fbc: str | None,
) -> None:
    service = MetaPixelService()
    await service.track_registration(
        professional_id=professional_id,
        email=email,
        name=name,
        phone=phone,
        client_ip=client_ip,
        client_user_agent=client_user_agent,
        fbp=fbp,
        fbc=fbc,
    )
    await service.track_start_trial(
        professional_id=professional_id,
        email=email,
        name=name,
        phone=phone,
        client_ip=client_ip,
        client_user_agent=client_user_agent,
        fbp=fbp,
        fbc=fbc,
    )


def _request_ip(request: Request) -> str:
    return get_client_ip(request)


def send_password_reset_email_task(to_email: str, user_name: str, raw_token: str) -> None:
    send_password_reset_email_sync(to_email=to_email, user_name=user_name, raw_token=raw_token)


def send_email_verification_email_task(to_email: str, user_name: str, raw_token: str) -> None:
    send_email_verification_email_sync(to_email=to_email, user_name=user_name, raw_token=raw_token)


def send_new_account_notification_task(
    user_name: str,
    user_email: str,
    specialty: str,
    council: str,
    phone: str,
    created_at: datetime,
    trial_ends_at: datetime | None,
) -> None:
    send_new_account_notification_sync(
        user_name=user_name,
        user_email=user_email,
        specialty=specialty,
        council=council,
        phone=phone,
        created_at=created_at,
        trial_ends_at=trial_ends_at,
    )


async def _issue_tokens(
    db: AsyncSession,
    professional: Professional,
) -> tuple[str, str]:
    access_token = create_access_token(professional.id, professional.token_version)
    refresh_token = await create_refresh_session(db, professional)
    await db.commit()
    return access_token, refresh_token


def _token_response() -> TokenResponse:
    # JWTs stay in HttpOnly cookies only — never echo usable tokens in JSON.
    return TokenResponse(
        access_token="",
        refresh_token="",
    )


def _apply_auth_cookies(response: Response, access_token: str, refresh_token: str) -> TokenResponse:
    set_auth_cookies(response, access_token, refresh_token)
    return _token_response()


def _resolve_refresh_token(request: Request, body: RefreshRequest) -> str:
    cookie_token = request.cookies.get(REFRESH_COOKIE, "").strip()
    body_token = (body.refresh_token or "").strip()
    token = cookie_token or body_token
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token inválido")
    return token


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(
    body: RegisterRequest,
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    enforce_register_rate_limit(_request_ip(request))
    existing = await db.execute(select(Professional).where(Professional.email == body.email))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="E-mail já cadastrado")
    settings = get_settings()
    now = datetime.now(UTC)
    trial_days = settings.trial_days
    professional = Professional(
        email=body.email,
        password_hash=hash_password(body.password),
        name=body.name,
        specialty_key=body.specialty_key,
        specialty=specialty_label(body.specialty_key),
        council=body.council,
        phone=body.phone,
        cpf=body.cpf or "",
        subscription_status="trialing",
        trial_started_at=now,
        trial_ends_at=now + timedelta(days=trial_days),
    )
    db.add(professional)
    await db.flush()
    db.add(
        Patient(
            professional_id=professional.id,
            name=DEMO_PATIENT_NAME,
            birth_date=demo_patient_birth_date(),
            diagnosis_keys=[],
            status="avaliacao",
            start_date=date.today(),
            avatar_color=DEMO_AVATAR_COLOR,
        )
    )
    access_token, refresh_token = await _issue_tokens(db, professional)
    raw_token = await request_email_verification(db, professional, force=True)
    if raw_token is not None:
        background_tasks.add_task(
            send_email_verification_email_task,
            professional.email,
            professional.name,
            raw_token,
        )
    background_tasks.add_task(
        send_new_account_notification_task,
        professional.name,
        professional.email,
        professional.specialty,
        professional.council,
        professional.phone,
        now,
        professional.trial_ends_at,
    )
    background_tasks.add_task(
        track_registration_events_task,
        str(professional.id),
        professional.email,
        professional.name,
        professional.phone,
        _request_ip(request),
        request.headers.get("user-agent"),
        request.cookies.get("_fbp"),
        request.cookies.get("_fbc"),
    )
    return _apply_auth_cookies(response, access_token, refresh_token)


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    enforce_login_rate_limit(_request_ip(request), body.email)
    result = await db.execute(select(Professional).where(Professional.email == body.email))
    professional = result.scalar_one_or_none()
    if not professional or not verify_password(body.password, professional.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Credenciais inválidas")
    if professional.is_disabled:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Conta desativada")
    access_token, refresh_token = await _issue_tokens(db, professional)
    if professional.email_verified_at is None:
        raw_token = await request_email_verification(db, professional, force=False)
        if raw_token is not None:
            background_tasks.add_task(
                send_email_verification_email_task,
                professional.email,
                professional.name,
                raw_token,
            )
    return _apply_auth_cookies(response, access_token, refresh_token)

@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    request: Request,
    response: Response,
    body: RefreshRequest,
    db: AsyncSession = Depends(get_db),
):
    raw_token = _resolve_refresh_token(request, body)
    professional, new_refresh = await rotate_refresh_session(db, raw_token)
    access_token = create_access_token(professional.id, professional.token_version)
    await db.commit()
    return _apply_auth_cookies(response, access_token, new_refresh)


@router.post("/logout", response_model=MessageResponse)
async def logout(
    request: Request,
    response: Response,
    body: RefreshRequest,
    db: AsyncSession = Depends(get_db),
):
    raw_token = request.cookies.get(REFRESH_COOKIE, "").strip() or (body.refresh_token or "").strip()
    if raw_token:
        await revoke_refresh_session(db, raw_token)
        await db.commit()
    clear_auth_cookies(response)
    return MessageResponse(message="Sessão encerrada")


@router.post("/logout-all", response_model=MessageResponse)
async def logout_all(
    response: Response,
    professional: Professional = Depends(get_current_professional),
    db: AsyncSession = Depends(get_db),
):
    await revoke_all_refresh_sessions(db, professional)
    await db.commit()
    clear_auth_cookies(response)
    return MessageResponse(message="Todas as sessões foram encerradas")


@router.post("/forgot-password", response_model=MessageResponse)
async def forgot_password(
    body: ForgotPasswordRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    enforce_forgot_rate_limit(_request_ip(request), body.email)
    result = await request_password_reset(db, body.email)
    if result is not None:
        professional, raw_token = result
        background_tasks.add_task(
            send_password_reset_email_task,
            professional.email,
            professional.name,
            raw_token,
        )
    return MessageResponse(message=GENERIC_FORGOT_MESSAGE)


@router.post("/reset-password", response_model=MessageResponse)
async def reset_password(
    body: ResetPasswordRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    enforce_reset_rate_limit(_request_ip(request))
    await reset_password_with_token(db=db, raw_token=body.token, new_password=body.new_password)
    return MessageResponse(message="Senha redefinida com sucesso")


@router.post("/change-password", response_model=MessageResponse)
async def change_current_password(
    body: ChangePasswordRequest,
    professional: Professional = Depends(get_current_professional),
    db: AsyncSession = Depends(get_db),
):
    await change_password(
        db=db,
        professional=professional,
        current_password=body.current_password,
        new_password=body.new_password,
    )
    return MessageResponse(message="Senha alterada com sucesso")


@router.post("/verify-email", response_model=MessageResponse)
async def verify_email(
    body: VerifyEmailRequest,
    db: AsyncSession = Depends(get_db),
):
    await verify_email_with_token(db, body.token)
    return MessageResponse(message="E-mail verificado com sucesso")


@router.post("/resend-verification", response_model=MessageResponse)
async def resend_verification(
    background_tasks: BackgroundTasks,
    professional: Professional = Depends(get_current_professional),
    db: AsyncSession = Depends(get_db),
):
    raw_token = await request_email_verification(db, professional, force=False)
    if raw_token is not None:
        background_tasks.add_task(
            send_email_verification_email_task,
            professional.email,
            professional.name,
            raw_token,
        )
    return MessageResponse(message="Se necessário, enviamos um novo link de verificação")
