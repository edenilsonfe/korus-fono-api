"""F17/4.3 — janitor de blobs: reserva, resolução e limpeza de órfãos.

Fluxo (plano §3.4/§6.2):

1. ``reserve_storage_cleanup`` registra a intenção de limpeza ANTES do upload,
   com commit próprio: se o processo cair entre o upload e a associação, a
   reserva sobrevive e o objeto órfão é removido depois.
2. A transação que associa o blob (upload concluído → chave persistida no
   recurso) chama ``resolve_storage_cleanup`` e retira a reserva.
3. ``queue_storage_cleanup`` enfileira na MESMA transação efeitos compensatórios
   que só podem ocorrer após o commit (ex.: blob anterior após substituição, ou
   blob de recurso excluído).
4. ``run_storage_cleanup`` (cron ARQ, lote limitado a 100) revalida referência
   viva antes de remover, trata objeto ausente como sucesso, faz retentativa
   limitada com backoff e deixa falha além do limite em ``failed`` — visível ao
   operador, nunca descarte silencioso. Nenhum log/erro carrega a chave crua.

O janitor só age sobre chaves registradas por estas funções — assets legados de
branding/F1 nunca entram na fila — e nunca apaga prefixo/bucket em lote.
"""

import hashlib
import logging
import re
from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.utils import utcnow
from app.models.attachment import Attachment
from app.models.home_program import HomeProgramPhoto
from app.models.professional import Professional
from app.models.resource import Resource
from app.models.storage_cleanup import (
    STORAGE_CLEANUP_DELETED,
    STORAGE_CLEANUP_FAILED,
    STORAGE_CLEANUP_KEPT,
    STORAGE_CLEANUP_PENDING,
    STORAGE_CLEANUP_RESOLVED,
    StorageCleanupTask,
)
from app.services.storage import storage_service

logger = logging.getLogger(__name__)

# Lote do cron: nunca mais que 100 chaves por execução.
STORAGE_CLEANUP_BATCH_LIMIT = 100
# Retentativas antes de marcar falha visível ao operador.
MAX_CLEANUP_ATTEMPTS = 5
_BACKOFF_BASE = timedelta(minutes=5)
_BACKOFF_MAX = timedelta(hours=24)

# Qualquer caminho de objeto em log/erro é substituído antes de sair.
_STORAGE_PATH_RE = re.compile(r"(?:resources|patients)/[^\s\"'<>]+")


def cleanup_backoff(attempts: int) -> timedelta:
    """Backoff exponencial limitado a 24h para a enésima tentativa (1-based)."""
    exponent = max(attempts - 1, 0)
    seconds = min(_BACKOFF_BASE.total_seconds() * (2**exponent), _BACKOFF_MAX.total_seconds())
    return timedelta(seconds=seconds)


def redact_storage_key(storage_key: str) -> str:
    """Identificador estável da chave, sem expor o valor (logs externos/Sentry)."""
    digest = hashlib.sha256(storage_key.encode("utf-8")).hexdigest()[:12]
    return f"key:{digest}"


def sanitize_storage_error(
    exc: BaseException, *, storage_key: str | None = None
) -> str:
    """Diagnóstico do erro sem chave/bucket — o que fica em ``last_error``."""
    message = str(exc) or ""
    settings = get_settings()
    for secret in (storage_key, settings.s3_bucket):
        if secret:
            message = message.replace(secret, "[redacted]")
    message = _STORAGE_PATH_RE.sub("[redacted-key]", message)
    message = " ".join(message.split())
    if len(message) > 300:
        message = message[:297] + "..."
    summary = type(exc).__name__
    return f"{summary}: {message}" if message else summary


def _validate_reservable_key(storage_key: str) -> None:
    """A chave é sempre gerada pelo servidor; aqui só se bloqueia valor inválido."""
    if not storage_key or not storage_key.strip():
        raise ValueError("storage_key obrigatória para o agendamento de limpeza.")
    if storage_key.startswith("/") or storage_key.endswith("/"):
        raise ValueError("storage_key inválida para o agendamento de limpeza.")
    if any(part in ("", ".", "..") for part in storage_key.split("/")):
        raise ValueError("storage_key inválida para o agendamento de limpeza.")


def queue_storage_cleanup(
    db: AsyncSession,
    storage_key: str,
    *,
    reason: str,
    professional_id=None,
) -> StorageCleanupTask:
    """Enfileira a limpeza na MESMA transação do efeito que a motiva.

    Usado quando a remoção só pode acontecer depois do commit (recurso
    excluído, blob anterior de uma substituição): se a transação reverter, a
    reserva reverte junto — nunca se remove um blob ainda referenciado.
    """
    _validate_reservable_key(storage_key)
    task = StorageCleanupTask(
        storage_key=storage_key,
        status=STORAGE_CLEANUP_PENDING,
        reason=reason,
        attempts=0,
        not_before=utcnow(),
        created_by_professional_id=professional_id,
    )
    db.add(task)
    return task


async def reserve_storage_cleanup(
    db: AsyncSession,
    storage_key: str,
    *,
    reason: str,
    professional_id=None,
) -> StorageCleanupTask:
    """Reserva de limpeza ANTES do upload — com commit próprio.

    O commit é intencional: a reserva precisa sobreviver a uma queda do
    processo entre o upload e a transação que associa o blob.
    """
    task = queue_storage_cleanup(
        db, storage_key, reason=reason, professional_id=professional_id
    )
    await db.commit()
    return task


def resolve_storage_cleanup(task: StorageCleanupTask | None) -> None:
    """Retira a reserva dentro da transação que associa o blob (sem commit).

    Se a transação reverter, a reserva volta a valer e o janitor remove o
    objeto — o caminho seguro para um upload que não chegou ao banco.
    """
    if task is not None and task.status == STORAGE_CLEANUP_PENDING:
        task.status = STORAGE_CLEANUP_RESOLVED


async def storage_key_in_use(db: AsyncSession, storage_key: str) -> bool:
    """FK viva apontando para a chave (recurso, anexo F1, branding F2 ou foto F16).

    A foto familiar (F16) entra como referência viva do prefixo
    ``patients/<id>/home-programs/...``: foto vigente protege o blob; foto já
    marcada como ``deleted`` (substituída/removida) libera a limpeza.
    """
    checks = (
        select(Resource.id).where(Resource.storage_key == storage_key).limit(1),
        select(Attachment.id).where(Attachment.storage_key == storage_key).limit(1),
        select(Professional.id)
        .where(
            or_(
                Professional.branding_logo_key == storage_key,
                Professional.branding_signature_key == storage_key,
            )
        )
        .limit(1),
        select(HomeProgramPhoto.id)
        .where(
            HomeProgramPhoto.storage_key == storage_key,
            HomeProgramPhoto.status != "deleted",
        )
        .limit(1),
    )
    for statement in checks:
        if (await db.execute(statement)).first() is not None:
            return True
    return False


async def run_storage_cleanup(
    db: AsyncSession,
    *,
    limit: int = STORAGE_CLEANUP_BATCH_LIMIT,
    now: datetime | None = None,
) -> dict[str, int]:
    """Processa um lote de reservas pendentes (janitor do cron ARQ).

    - lote limitado a ``STORAGE_CLEANUP_BATCH_LIMIT`` (100) por execução;
    - revalida referência viva antes de remover (nunca apaga objeto associado);
    - ``StorageService.delete`` trata objeto ausente como sucesso;
    - falha transitória: ``attempts`` + backoff exponencial, nova tentativa;
    - falha além de ``MAX_CLEANUP_ATTEMPTS``: status ``failed`` com log de
      ERROR (sem chave crua) — visível ao operador, nunca descarte silencioso.

    Devolve o resumo do lote: ``selected``/``deleted``/``kept``/``retried``/
    ``failed``.
    """
    moment = now or utcnow()
    batch_size = max(0, min(int(limit), STORAGE_CLEANUP_BATCH_LIMIT))
    summary = {"selected": 0, "deleted": 0, "kept": 0, "retried": 0, "failed": 0}
    if batch_size == 0:
        return summary

    tasks = (
        (
            await db.execute(
                select(StorageCleanupTask)
                .where(
                    StorageCleanupTask.status == STORAGE_CLEANUP_PENDING,
                    StorageCleanupTask.not_before <= moment,
                )
                .order_by(StorageCleanupTask.not_before, StorageCleanupTask.id)
                .limit(batch_size)
            )
        )
        .scalars()
        .all()
    )
    summary["selected"] = len(tasks)

    for task in tasks:
        if await storage_key_in_use(db, task.storage_key):
            task.status = STORAGE_CLEANUP_KEPT
            summary["kept"] += 1
            logger.info("Storage cleanup kept referenced blob: task=%s", task.id)
            await db.commit()
            continue

        try:
            await storage_service.delete(task.storage_key)
        except Exception as exc:  # noqa: BLE001 — falha vira retry/falha visível
            task.attempts += 1
            task.last_error = sanitize_storage_error(exc, storage_key=task.storage_key)
            if task.attempts >= MAX_CLEANUP_ATTEMPTS:
                task.status = STORAGE_CLEANUP_FAILED
                summary["failed"] += 1
                logger.error(
                    "Storage cleanup exhausted retries: task=%s attempts=%s key=%s",
                    task.id,
                    task.attempts,
                    redact_storage_key(task.storage_key),
                )
            else:
                task.not_before = moment + cleanup_backoff(task.attempts)
                summary["retried"] += 1
                logger.warning(
                    "Storage cleanup retry scheduled: task=%s attempts=%s",
                    task.id,
                    task.attempts,
                )
            await db.commit()
            continue

        task.status = STORAGE_CLEANUP_DELETED
        task.last_error = None
        summary["deleted"] += 1
        await db.commit()

    return summary
