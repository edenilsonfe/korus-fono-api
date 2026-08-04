"""Route test: same-origin attachment streaming (preview iframe/img)."""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.attachment import Attachment
from app.services.storage import storage_service


@pytest.fixture
async def attachment(db_session: AsyncSession, patient, professional):
    from app.core.utils import utcnow

    att = Attachment(
        patient_id=patient.id,
        professional_id=professional.id,
        name="exame.pdf",
        category="relatorio",
        size_bytes=11,
        storage_key=f"patients/{patient.id}/x/exame.pdf",
        date=utcnow(),
    )
    db_session.add(att)
    await db_session.commit()
    await db_session.refresh(att)
    return att


async def test_attachment_file_streams_inline(api_client: AsyncClient, attachment, auth_headers):
    pdf_body = b"%PDF-1.7 test"

    async def fake_download(key):
        return (pdf_body, "application/pdf")

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(storage_service, "download", fake_download)
    try:
        res = await api_client.get(
            f"/api/v1/patients/{attachment.patient_id}/attachments/{attachment.id}/file",
            headers=auth_headers,
        )
    finally:
        monkeypatch.undo()

    assert res.status_code == 200
    assert res.headers["content-type"] == "application/pdf"
    assert res.headers["content-disposition"].startswith('inline; filename="exame.pdf"')
    assert res.content == pdf_body


async def test_attachment_file_requires_ownership(
    api_client: AsyncClient, db_session: AsyncSession, professional, patient, auth_headers
):
    from app.core.security import create_access_token, hash_password
    from app.core.utils import utcnow
    from app.models.professional import Professional

    stranger = Professional(
        email="stranger@example.com",
        password_hash=hash_password("testpass123"),
        name="Dra. Outra",
        specialty_key="fono",
        specialty="Fonoaudiologia",
        council="CREFITO",
        phone="11999991111",
        email_verified_at=utcnow(),
    )
    db_session.add(stranger)
    await db_session.flush()

    att = Attachment(
        patient_id=patient.id,
        professional_id=professional.id,
        name="privado.pdf",
        category="relatorio",
        size_bytes=4,
        storage_key=f"patients/{patient.id}/x/privado.pdf",
        date=utcnow(),
    )
    db_session.add(att)
    await db_session.commit()
    await db_session.refresh(att)

    res = await api_client.get(
        f"/api/v1/patients/{patient.id}/attachments/{att.id}/file",
        headers={"Authorization": f"Bearer {create_access_token(stranger.id)}"},
    )
    assert res.status_code == 404  # profissional estranho não acessa o anexo
