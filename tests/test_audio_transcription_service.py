from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, UploadFile


def _upload(body: bytes, *, filename: str = "sessao.mp3", content_type: str = "audio/mpeg"):
    from io import BytesIO

    return UploadFile(filename=filename, file=BytesIO(body), headers={"content-type": content_type})


@pytest.mark.asyncio
async def test_transcribe_audio_sends_the_selected_file_to_the_provider():
    from app.services.audio_transcription_service import transcribe_audio

    provider = SimpleNamespace(
        audio=SimpleNamespace(
            transcriptions=SimpleNamespace(
                create=AsyncMock(return_value=SimpleNamespace(text="fala transcrita da sessão"))
            )
        )
    )
    settings = SimpleNamespace(
        audio_transcription_api_key="real-key",
        audio_transcription_base_url="https://api.openai.com/v1",
        audio_transcription_model="gpt-4o-mini-transcribe",
        audio_transcription_max_bytes=1024,
    )

    result = await transcribe_audio(_upload(b"ID3-audio-real"), settings=settings, client=provider)

    assert result.text == "fala transcrita da sessão"
    assert result.filename == "sessao.mp3"
    assert result.size_bytes == len(b"ID3-audio-real")
    assert len(result.sha256) == 64
    provider.audio.transcriptions.create.assert_awaited_once()
    request = provider.audio.transcriptions.create.await_args.kwargs
    assert request["model"] == "gpt-4o-mini-transcribe"
    assert request["file"][0] == "sessao.mp3"
    assert request["file"][1] == b"ID3-audio-real"


@pytest.mark.asyncio
async def test_transcribe_audio_rejects_missing_provider_configuration():
    from app.services.audio_transcription_service import transcribe_audio

    settings = SimpleNamespace(
        audio_transcription_api_key="",
        audio_transcription_base_url="https://api.openai.com/v1",
        audio_transcription_model="gpt-4o-mini-transcribe",
        audio_transcription_max_bytes=1024,
    )

    with pytest.raises(HTTPException) as exc_info:
        await transcribe_audio(_upload(b"ID3-audio-real"), settings=settings)

    assert exc_info.value.status_code == 503
    assert "transcrição de áudio" in exc_info.value.detail.lower()


@pytest.mark.asyncio
async def test_transcribe_audio_rejects_non_audio_and_oversized_files():
    from app.services.audio_transcription_service import transcribe_audio

    settings = SimpleNamespace(
        audio_transcription_api_key="real-key",
        audio_transcription_base_url="https://api.openai.com/v1",
        audio_transcription_model="gpt-4o-mini-transcribe",
        audio_transcription_max_bytes=4,
    )

    with pytest.raises(HTTPException) as wrong_type:
        await transcribe_audio(
            _upload(b"texto", filename="notas.txt", content_type="text/plain"), settings=settings
        )
    assert wrong_type.value.status_code == 415

    with pytest.raises(HTTPException) as too_large:
        await transcribe_audio(_upload(b"12345"), settings=settings)
    assert too_large.value.status_code == 413
