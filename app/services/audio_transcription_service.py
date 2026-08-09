"""Validated audio ingestion and speech-to-text provider boundary."""

import hashlib
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, UploadFile, status

from app.core.config import Settings, get_settings
from app.services.attachment_upload import assert_declared_matches_sniff, sanitize_filename


ALLOWED_AUDIO_CONTENT_TYPES = frozenset(
    {
        "audio/mpeg",
        "audio/mp4",
        "audio/x-m4a",
        "audio/wav",
        "audio/x-wav",
        "audio/webm",
        "audio/ogg",
    }
)


@dataclass(frozen=True)
class AudioTranscription:
    text: str
    filename: str
    content_type: str
    size_bytes: int
    sha256: str


async def _read_limited(file: UploadFile, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(min(1024 * 1024, max_bytes + 1)):
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=f"O áudio excede o limite de {max_bytes // (1024 * 1024)} MB.",
            )
        chunks.append(chunk)
    return b"".join(chunks)


async def transcribe_audio(
    file: UploadFile,
    *,
    settings: Settings | Any | None = None,
    client: Any | None = None,
) -> AudioTranscription:
    settings = settings or get_settings()
    if not settings.audio_transcription_api_key.strip():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Serviço de transcrição de áudio não configurado.",
        )

    content_type = (file.content_type or "").lower().strip()
    if content_type not in ALLOWED_AUDIO_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Formato de áudio não suportado. Envie MP3, WAV, M4A, WebM ou OGG.",
        )

    filename = sanitize_filename(file.filename or "audio")
    body = await _read_limited(file, settings.audio_transcription_max_bytes)
    if not body:
        raise HTTPException(status_code=422, detail="O arquivo de áudio está vazio.")
    assert_declared_matches_sniff(content_type, body)

    if client is None:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(
            api_key=settings.audio_transcription_api_key,
            base_url=settings.audio_transcription_base_url,
            timeout=settings.assistant_llm_timeout_seconds,
        )

    try:
        response = await client.audio.transcriptions.create(
            model=settings.audio_transcription_model,
            file=(filename, body, content_type),
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Não foi possível transcrever o áudio no momento.",
        ) from exc

    text = response if isinstance(response, str) else getattr(response, "text", "")
    text = str(text or "").strip()
    if not text:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="O serviço de transcrição não retornou conteúdo.",
        )

    return AudioTranscription(
        text=text,
        filename=filename,
        content_type=content_type,
        size_bytes=len(body),
        sha256=hashlib.sha256(body).hexdigest(),
    )
