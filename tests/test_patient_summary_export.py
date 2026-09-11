"""F6 — GET /patients/{id}/export.pdf: resumo B do design B do spike 017.

O resumo é minimizado: identificação/diagnósticos, metas atuais e as últimas N
sessões (tipo/data, mais recentes primeiro). Nunca carrega anamnese, corpo de
evoluções, notas de sessão ou anexos — é um artefato de continuidade do
cuidado, não o prontuário completo.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.security import create_access_token
from app.models.anamnese import AnamneseEntry
from app.models.attachment import Attachment
from app.models.evolution import Evolution
from app.models.goal import Goal
from app.models.patient import Patient
from app.models.professional import Professional
from app.models.session import Session

ANAMNESE_MARKER = "ANAMNESE-RESTRITA-9f2"
EVOLUTION_MARKER = "EVOLUCAO-RESTRITA-9f3"
ATTACHMENT_MARKER = "anexo-restrito-9f4"
SESSION_NOTES_MARKER = "NOTA-CLINICA-SESSAO-9f5"
PATIENT_NOTES_MARKER = "OBSERVACAO-PACIENTE-9f6"


@pytest.fixture
def audit_session_factory(db_session, monkeypatch):
    """Auditoria usa uma fábrica de sessão própria; nos testes aponta para o
    mesmo banco SQLite in-memory do app (mesmo padrão do middleware)."""
    factory = async_sessionmaker(
        db_session.bind, class_=AsyncSession, expire_on_commit=False
    )
    monkeypatch.setattr(
        "app.services.patient_record_export.AsyncSessionLocal", factory
    )
    return factory


def pdf_stream_text(data: bytes) -> str:
    """Extrai as strings de texto dos content streams do PDF (ASCII85+Flate).

    Sem pypdf: só stdlib. Descomprime os streams do ReportLab e decodifica os
    escapes de string PDF (``\\(``, ``\\)``, octais como ``\\343``), devolvendo
    uma linha por literal — suficiente para provar presença/ausência de
    marcadores e a ordem das linhas no documento.
    """
    import base64
    import re
    import zlib

    def _decode_stream(raw: bytes) -> bytes:
        if raw.endswith(b"~>"):
            decoded = base64.a85decode(raw[:-2], adobe=False)
            try:
                return zlib.decompress(decoded)
            except zlib.error:
                return decoded
        try:
            return zlib.decompress(raw)
        except zlib.error:
            return raw

    def _unescape(literal: str) -> str:
        out: list[str] = []
        index = 0
        while index < len(literal):
            char = literal[index]
            if char != "\\":
                out.append(char)
                index += 1
                continue
            index += 1
            if index >= len(literal):
                break
            nxt = literal[index]
            if nxt.isdigit():
                digits = nxt
                index += 1
                while index < len(literal) and len(digits) < 3 and literal[index].isdigit():
                    digits += literal[index]
                    index += 1
                out.append(chr(int(digits, 8)))
                continue
            mapped = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f"}
            out.append(mapped.get(nxt, nxt))
            index += 1
        return "".join(out)

    lines: list[str] = []
    for match in re.finditer(rb"stream\r?\n(.*?)endstream", data, re.S):
        content = _decode_stream(match.group(1).strip())
        for literal in re.finditer(rb"\((?:[^()\\]|\\.)*\)", content, re.S):
            lines.append(_unescape(literal.group(0)[1:-1].decode("latin-1")))
    return "\n".join(lines)


async def _add_goal(db_session, patient, **overrides):
    goal = Goal(
        patient_id=patient.id,
        professional_id=patient.professional_id,
        title=overrides.get("title", "Meta de linguagem"),
        area=overrides.get("area", "Linguagem"),
        progress=overrides.get("progress", 40),
        status=overrides.get("status", "Em andamento"),
        start_date=overrides.get("start_date", datetime(2026, 2, 1).date()),
    )
    db_session.add(goal)
    await db_session.commit()
    await db_session.refresh(goal)
    return goal


async def _add_session(db_session, patient, *, date, type_, notes="", objectives=None):
    session = Session(
        patient_id=patient.id,
        professional_id=patient.professional_id,
        date=date,
        duration=50,
        type=type_,
        objectives=objectives or [],
        notes=notes,
    )
    db_session.add(session)
    await db_session.commit()
    await db_session.refresh(session)
    return session


async def _add_forbidden_content(db_session, patient):
    """Dados que o design B jamais inclui, com marcadores rastreáveis."""
    db_session.add(
        AnamneseEntry(patient_id=patient.id, section="queixa", value=ANAMNESE_MARKER)
    )
    db_session.add(
        Evolution(
            patient_id=patient.id,
            professional_id=patient.professional_id,
            date=datetime(2026, 3, 1, 10, 0, tzinfo=UTC),
            title="Evolução",
            content=EVOLUTION_MARKER,
        )
    )
    db_session.add(
        Attachment(
            patient_id=patient.id,
            professional_id=patient.professional_id,
            name=f"{ATTACHMENT_MARKER}.pdf",
            category="relatorio",
            size_bytes=10,
            storage_key=f"patients/attachments/{ATTACHMENT_MARKER}.pdf",
            date=datetime(2026, 3, 2, 10, 0, tzinfo=UTC),
        )
    )
    patient.notes = PATIENT_NOTES_MARKER
    await db_session.commit()


@pytest.mark.asyncio
async def test_summary_pdf_has_identification_goals_and_sessions(
    api_client, auth_headers, db_session, patient, professional, audit_session_factory
):
    await _add_goal(db_session, patient, title="Meta de linguagem", progress=40)
    await _add_session(
        db_session, patient, date=datetime(2026, 3, 15, 14, 0), type_="Terapia de linguagem"
    )

    response = await api_client.get(
        f"/api/v1/patients/{patient.id}/export.pdf", headers=auth_headers
    )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/pdf"
    assert (
        response.headers["content-disposition"]
        == f'attachment; filename="paciente-{patient.id}-resumo.pdf"'
    )
    assert response.content.startswith(b"%PDF")
    assert len(response.content) > 1000

    text = pdf_stream_text(response.content)
    assert "João Silva" in text
    assert "Transtorno do Espectro Autista" in text
    assert "Meta de linguagem" in text
    assert "40%" in text
    assert "15/03/2026 14:00" in text
    assert "Terapia de linguagem" in text
    assert "Resumo do paciente" in text
    assert professional.name.split()[0] in text


@pytest.mark.asyncio
async def test_summary_pdf_never_contains_forbidden_clinical_content(
    api_client, auth_headers, db_session, patient, audit_session_factory
):
    await _add_goal(db_session, patient)
    await _add_session(
        db_session,
        patient,
        date=datetime(2026, 3, 15, 14, 0),
        type_="Terapia",
        notes=SESSION_NOTES_MARKER,
        objectives=[SESSION_NOTES_MARKER],
    )
    await _add_forbidden_content(db_session, patient)

    response = await api_client.get(
        f"/api/v1/patients/{patient.id}/export.pdf", headers=auth_headers
    )

    assert response.status_code == 200, response.text
    text = pdf_stream_text(response.content)
    for marker in (
        ANAMNESE_MARKER,
        EVOLUTION_MARKER,
        ATTACHMENT_MARKER,
        SESSION_NOTES_MARKER,
        PATIENT_NOTES_MARKER,
    ):
        assert marker not in text, f"conteúdo vedado vazou no resumo: {marker}"


@pytest.mark.asyncio
async def test_summary_sessions_are_the_most_recent_in_descending_order_with_limit(
    api_client, auth_headers, db_session, patient, audit_session_factory
):
    await _add_session(
        db_session, patient, date=datetime(2026, 1, 10, 9, 0), type_="Avaliação inicial"
    )
    await _add_session(
        db_session, patient, date=datetime(2026, 2, 20, 10, 30), type_="Terapia de fala"
    )
    await _add_session(
        db_session, patient, date=datetime(2026, 3, 15, 14, 0), type_="Terapia de linguagem"
    )

    response = await api_client.get(
        f"/api/v1/patients/{patient.id}/export.pdf",
        headers=auth_headers,
        params={"sessionsLimit": 2},
    )

    assert response.status_code == 200, response.text
    text = pdf_stream_text(response.content)
    assert "15/03/2026 14:00" in text
    assert "20/02/2026 10:30" in text
    assert "10/01/2026 09:00" not in text
    assert text.index("15/03/2026 14:00") < text.index("20/02/2026 10:30")


@pytest.mark.asyncio
async def test_summary_default_limit_uses_ten_sessions(
    api_client, auth_headers, db_session, patient, audit_session_factory
):
    await _add_session(db_session, patient, date=datetime(2026, 1, 10, 9, 0), type_="A")
    await _add_session(db_session, patient, date=datetime(2026, 2, 20, 10, 30), type_="B")
    await _add_session(db_session, patient, date=datetime(2026, 3, 15, 14, 0), type_="C")

    response = await api_client.get(
        f"/api/v1/patients/{patient.id}/export.pdf", headers=auth_headers
    )

    assert response.status_code == 200, response.text
    text = pdf_stream_text(response.content)
    for marker in ("10/01/2026 09:00", "20/02/2026 10:30", "15/03/2026 14:00"):
        assert marker in text


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [0, 51, -1, "abc"])
async def test_summary_sessions_limit_out_of_range_returns_422(
    api_client, auth_headers, patient, audit_session_factory, value
):
    response = await api_client.get(
        f"/api/v1/patients/{patient.id}/export.pdf",
        headers=auth_headers,
        params={"sessionsLimit": value},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [1, 50])
async def test_summary_sessions_limit_bounds_are_accepted(
    api_client, auth_headers, patient, audit_session_factory, value
):
    response = await api_client.get(
        f"/api/v1/patients/{patient.id}/export.pdf",
        headers=auth_headers,
        params={"sessionsLimit": value},
    )
    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_summary_export_of_foreign_patient_returns_404(
    api_client, auth_headers, db_session, professional, audit_session_factory
):
    other = Professional(
        email="outro-resumo@test.com",
        password_hash="x",
        name="Outra profissional",
        specialty_key="fono",
        specialty="Fonoaudiologia",
    )
    db_session.add(other)
    await db_session.flush()
    foreign = Patient(
        professional_id=other.id,
        name="Paciente alheio",
        birth_date=datetime(2020, 1, 1).date(),
        diagnosis_keys=[],
        status="ativo",
        start_date=datetime(2026, 1, 1).date(),
        avatar_color="oklch(0.58 0.12 205)",
    )
    db_session.add(foreign)
    await db_session.commit()

    response = await api_client.get(
        f"/api/v1/patients/{foreign.id}/export.pdf", headers=auth_headers
    )

    assert response.status_code == 404
    assert "Paciente não encontrado" in response.json()["detail"]


@pytest.mark.asyncio
async def test_summary_export_requires_authentication(api_client, patient):
    response = await api_client.get(f"/api/v1/patients/{patient.id}/export.pdf")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_summary_export_from_other_professional_token_returns_404(
    api_client, db_session, patient, audit_session_factory
):
    other = Professional(
        email="token-alheio@test.com",
        password_hash="x",
        name="Outra profissional",
        specialty_key="fono",
        specialty="Fonoaudiologia",
        email_verified_at=datetime.now(UTC),
    )
    db_session.add(other)
    await db_session.commit()
    await db_session.refresh(other)
    headers = {"Authorization": f"Bearer {create_access_token(other.id)}"}

    response = await api_client.get(
        f"/api/v1/patients/{patient.id}/export.pdf", headers=headers
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_summary_export_without_history_still_renders(
    api_client, auth_headers, patient, audit_session_factory
):
    response = await api_client.get(
        f"/api/v1/patients/{patient.id}/export.pdf", headers=auth_headers
    )

    assert response.status_code == 200, response.text
    text = pdf_stream_text(response.content)
    assert "Nenhuma meta registrada." in text
    assert "Nenhuma sessão registrada." in text
