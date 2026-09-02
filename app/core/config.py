from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Korus Fono API"
    debug: bool = False
    # Exige ALLOW_DEBUG=true junto com DEBUG=true (dev/testes). Nunca em produção.
    allow_debug: bool = False
    api_v1_prefix: str = "/api/v1"
    cors_origins: str = (
        "http://localhost:3000,"
        "http://localhost:5173,"
        "http://localhost:4173,"
        "http://127.0.0.1:3000,"
        "http://127.0.0.1:5173,"
        "http://127.0.0.1:4173"
    )

    database_url: str = "postgresql+asyncpg://korus:korus@localhost:5433/korus_one"

    @field_validator("database_url", mode="before")
    @classmethod
    def normalize_database_url(cls, value: object) -> str:
        """Railway Postgres costuma entregar postgresql://; asyncpg exige +asyncpg."""
        url = str(value or "").strip()
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://") :]
        if url.startswith("postgresql://") and not url.startswith("postgresql+asyncpg://"):
            url = "postgresql+asyncpg://" + url[len("postgresql://") :]
        return url

    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 7
    auth_rate_limit_fail_closed: bool = True
    # How many reverse-proxy hops to skip from the right of X-Forwarded-For
    # when deriving the client IP (auth rate-limit, billing remote_ip, etc.).
    # Default 1 = single trusted proxy (e.g. Railway / Cloudflare edge).
    trusted_proxy_count: int = 1

    redis_url: str = "redis://localhost:6380"

    resend_api_key: str = ""
    email_from: str = "Korus Fono <noreply@korusfono.com.br>"
    email_sending_enabled: bool = False
    password_token_expire_minutes: int = 60
    password_reset_cooldown_seconds: int = 60
    email_verification_expire_minutes: int = 1440
    email_verification_cooldown_seconds: int = 60
    # Evita reenvio acidental da mesma régua de trial ao mesmo usuário.
    trial_email_resend_cooldown_hours: int = 24
    # Destinatário do aviso interno de novo cadastro (vazio = desligado).
    new_account_notification_email: str = ""
    # Destinatário do canal de suporte (form de contato; vazio = desligado).
    support_contact_email: str = ""

    # Vazio = AWS S3 real. Dev local (docker-compose / .env): http://localhost:9000 (MinIO).
    s3_endpoint: str = ""
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_bucket: str = "korus-attachments"
    s3_region: str = "us-east-1"

    @property
    def s3_endpoint_url(self) -> str | None:
        endpoint = (self.s3_endpoint or "").strip()
        return endpoint or None

    opencode_api_key: str = ""
    opencode_base_url: str = "https://opencode.ai/zen/v1"
    opencode_model: str = "deepseek-v4-flash"

    # Speech-to-text usa um provedor dedicado; OpenCode Zen expõe apenas modelos de texto.
    audio_transcription_api_key: str = ""
    audio_transcription_base_url: str = "https://api.openai.com/v1"
    audio_transcription_model: str = "gpt-4o-mini-transcribe"
    audio_transcription_max_bytes: int = 25 * 1024 * 1024

    # Assistente de IA unificado (clínico + gestão) com tool-calling.
    assistant_rate_limit_per_hour: int = 30
    assistant_llm_timeout_seconds: int = 120
    ai_context_max_chars: int = 12000

    whatsapp_provider: str = "evolution"
    whatsapp_credential_encryption_key: str = ""
    app_public_url: str = ""
    evolution_api_base_url: str = "http://localhost:8080"
    evolution_global_api_key: str = ""
    evolution_webhook_secret: str = ""
    # Instância Evolution da plataforma usada para enviar a mensagem de boas-vindas
    # após o cadastro (vazio = recurso desativado). Envia com a chave global.
    evolution_welcome_instance_name: str = ""
    clinic_timezone: str = "America/Sao_Paulo"
    google_calendar_client_id: str = ""
    google_calendar_client_secret: str = ""
    google_calendar_credential_encryption_key: str = ""
    whatsapp_scheduler_interval_seconds: int = 900
    whatsapp_reminder_window_hours: int = 24
    whatsapp_reminder_tolerance_minutes: int = 15
    whatsapp_reminder_reschedule_cooldown_hours: int = 6
    whatsapp_billing_reminder_days_before: int = 3

    billing_provider: str = "stub"
    asaas_api_key: str = ""
    asaas_api_base_url: str = "https://api-sandbox.asaas.com/v3"
    asaas_webhook_token: str = ""
    affiliate_payout_encryption_key: str = ""
    affiliate_cash_payouts_enabled: bool = False
    affiliate_payout_single_operator_pilot: bool = False
    frontend_url: str = "http://localhost:5173"
    trial_days: int = 7

    # Meta Pixel / Conversions API (server-side tracking). Vazio desliga.
    meta_pixel_id: str = ""
    meta_capi_access_token: str = ""
    # Código de evento de teste gerado em Event Manager > Test Events (só dev).
    meta_capi_test_event_code: str = ""
    meta_graph_api_version: str = "v21.0"

    # Project token público para eventos de conversão server-side. Vazio desliga.
    posthog_project_token: str = ""
    posthog_host: str = "https://us.i.posthog.com"

    instrument_packages_root: str = ""

    max_upload_bytes: int = 26214400

    sentry_dsn: str = ""
    sentry_environment: str = ""
    sentry_traces_sample_rate: float | None = None
    sentry_release: str = ""

    @field_validator("evolution_api_base_url", mode="before")
    @classmethod
    def normalize_evolution_api_base_url(cls, value: object) -> str:
        url = str(value or "").strip().rstrip("/")
        if not url:
            return url
        if url.startswith(("http://", "https://")):
            return url
        if url.startswith(("localhost", "127.0.0.1")):
            return f"http://{url}"
        return f"https://{url}"

    @property
    def effective_billing_provider(self) -> str:
        provider = self.billing_provider.lower().strip() or "stub"
        asaas_key = self.asaas_api_key.strip()
        if provider == "asaas":
            if not asaas_key:
                return "stub"
            # Chaves reais Asaas começam com $aact_; placeholders em dev usam stub.
            if self.debug and not asaas_key.startswith("$aact_"):
                return "stub"
            return "asaas"
        return provider

    @property
    def billing_frontend_base_url(self) -> str:
        return (self.frontend_url or "").rstrip("/") or self.cors_origin_list[0]

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def public_api_base_url(self) -> str | None:
        base = (self.app_public_url or "").strip()
        return base or None

    @property
    def evolution_webhook_url(self) -> str | None:
        base = self.public_api_base_url
        if not base:
            return None
        return f"{base.rstrip('/')}/api/v1/webhooks/evolution/whatsapp"

    @property
    def google_calendar_redirect_uri(self) -> str:
        base = (self.app_public_url or "").strip().rstrip("/")
        if not base:
            base = "http://localhost:8000"
        return f"{base}/api/v1/google-calendar/oauth/callback"

    @property
    def google_calendar_configured(self) -> bool:
        return bool(
            self.google_calendar_client_id.strip()
            and self.google_calendar_client_secret.strip()
            and self.google_calendar_credential_encryption_key.strip()
        )


INSECURE_JWT_SECRETS = frozenset({"change-me-in-production", ""})
PRODUCTION_SENTRY_ENVS = frozenset({"production", "prod"})


def _is_public_https_url(value: str) -> bool:
    try:
        parsed = urlsplit((value or "").strip())
        hostname = (parsed.hostname or "").rstrip(".").lower()
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and bool(hostname)
        and hostname not in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
        and not hostname.endswith(".localhost")
    )


def is_production_runtime(settings: Settings) -> bool:
    env = (settings.sentry_environment or "").strip().lower()
    return env in PRODUCTION_SENTRY_ENVS


def validate_settings(settings: Settings) -> None:
    google_values = (
        settings.google_calendar_client_id,
        settings.google_calendar_client_secret,
        settings.google_calendar_credential_encryption_key,
    )
    if any(value.strip() for value in google_values) and not all(
        value.strip() for value in google_values
    ):
        raise RuntimeError(
            "Configuração Google Calendar incompleta: defina CLIENT_ID, CLIENT_SECRET "
            "e CREDENTIAL_ENCRYPTION_KEY"
        )
    if is_production_runtime(settings):
        if settings.debug:
            raise RuntimeError(
                "DEBUG=true não é permitido com SENTRY_ENVIRONMENT de produção "
                "(production|prod)"
            )
        if settings.effective_billing_provider == "stub":
            raise RuntimeError(
                "Billing stub não é permitido em produção "
                "(SENTRY_ENVIRONMENT=production|prod); use BILLING_PROVIDER=asaas "
                "com ASAAS_API_KEY"
            )
        if not settings.opencode_api_key.strip():
            raise RuntimeError(
                "OPENCODE_API_KEY é obrigatória em produção para impedir respostas simuladas "
                "nas ferramentas de IA"
            )
        if settings.affiliate_cash_payouts_enabled:
            if settings.effective_billing_provider != "asaas":
                raise RuntimeError("Saques de afiliados exigem BILLING_PROVIDER=asaas")
            if not settings.affiliate_payout_encryption_key.strip():
                raise RuntimeError(
                    "AFFILIATE_PAYOUT_ENCRYPTION_KEY é obrigatória para saques de afiliados"
                )
    elif settings.debug and not settings.allow_debug:
        raise RuntimeError(
            "DEBUG=true exige ALLOW_DEBUG=true (não defina ALLOW_DEBUG em produção)"
        )

    if settings.debug:
        return
    if not _is_public_https_url(settings.frontend_url):
        raise RuntimeError(
            "FRONTEND_URL deve apontar para uma origem HTTPS pública com DEBUG=false"
        )
    secret = (settings.jwt_secret or "").strip()
    if secret in INSECURE_JWT_SECRETS or len(secret) < 32:
        raise RuntimeError(
            "JWT_SECRET inseguro ou ausente: defina um segredo forte com debug=False"
        )
    if settings.whatsapp_provider.strip().lower() == "evolution":
        missing: list[str] = []
        if not (settings.evolution_global_api_key or "").strip():
            missing.append("EVOLUTION_GLOBAL_API_KEY")
        if not (settings.evolution_webhook_secret or "").strip():
            missing.append("EVOLUTION_WEBHOOK_SECRET")
        if not (settings.whatsapp_credential_encryption_key or "").strip():
            missing.append("WHATSAPP_CREDENTIAL_ENCRYPTION_KEY")
        if not (settings.app_public_url or "").strip():
            missing.append("APP_PUBLIC_URL")
        if missing:
            raise RuntimeError(
                "Configuração Evolution incompleta com debug=False: "
                + ", ".join(missing)
            )


@lru_cache
def get_settings() -> Settings:
    return Settings()
