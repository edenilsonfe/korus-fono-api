"""PostgreSQL-only C1 invariants; skipped without TEST_AUDIT_PG_URL."""

import asyncio
from datetime import UTC, date, datetime, timedelta
from io import BytesIO
from uuid import uuid4

import pytest
from fastapi import HTTPException, UploadFile
from sqlalchemy import func, select

from app.models.anamnese import AnamneseEntry
from app.models.attachment import Attachment
from app.models.caregiver import Caregiver
from app.models.intake import IntakeFile, IntakeRequest
from app.models.intake import IntakeGrant
from app.models.patient import Patient
from app.models.professional import Professional
from app.schemas.intake import IntakeDraftPatch, IntakeReview
from app.schemas.prontuario import AnamneseEntryInput
from app.services import anamnese_service, intake_service
from app.utils.token_hash import hash_token


async def _seed(factory, *, status="draft"):
    async with factory() as db:
        pro = Professional(email=f"intake-pg-{uuid4().hex}@example.com", name="PG Test", password_hash="unused")
        db.add(pro)
        await db.flush()
        patient = Patient(professional_id=pro.id, name="Paciente PG", birth_date=date(2020, 1, 1), start_date=date.today(), avatar_color="teal", diagnosis_keys=[], status="ativo")
        db.add(patient)
        await db.flush()
        caregiver = Caregiver(patient_id=patient.id, name="Responsável PG", relation="Mãe", is_primary=True)
        db.add(caregiver)
        await db.flush()
        item = IntakeRequest(patient_id=patient.id, caregiver_id=caregiver.id, caregiver_name_snapshot=caregiver.name, owner_professional_id=pro.id, form_version="pediatric-v1", status=status, responses={"reasonForReferral": {"value": "Fala", "notKnown": False}}, version=1)
        db.add(item)
        await db.commit()
        return {"professional": pro.id, "patient": patient.id, "caregiver": caregiver.id, "request": item.id}


@pytest.mark.asyncio
async def test_intake_caregiver_delete_preserves_request_and_nulls_identity(audit_pg_factory):
    ids = await _seed(audit_pg_factory)
    async with audit_pg_factory() as db:
        caregiver = await db.get(Caregiver, ids["caregiver"])
        await db.delete(caregiver)
        await db.commit()
        request = await db.get(IntakeRequest, ids["request"])
        assert request is not None
        assert request.caregiver_id is None
        assert request.caregiver_name_snapshot == "Responsável PG"


@pytest.mark.asyncio
async def test_intake_concurrent_draft_saves_have_one_winner(audit_pg_factory, monkeypatch):
    async def enabled(*_args, **_kwargs):
        return None

    monkeypatch.setattr(intake_service, "_workflow_enabled", enabled)
    ids = await _seed(audit_pg_factory)

    async def save(value):
        async with audit_pg_factory() as db:
            request = await db.get(IntakeRequest, ids["request"])
            await asyncio.sleep(0.05)
            try:
                await intake_service.save_draft(db, raw_token="test", request=request, body=IntakeDraftPatch(expected_version=1, responses={"reasonForReferral": {"value": value, "notKnown": False}}))
                await db.commit()
                return "saved"
            except HTTPException as exc:
                await db.rollback()
                return exc.status_code

    result = await asyncio.gather(save("A"), save("B"))
    assert sorted(result, key=str) == [409, "saved"]


@pytest.mark.asyncio
async def test_intake_import_conflicts_with_locked_anamnese_writer(audit_pg_factory, monkeypatch):
    async def enabled(*_args, **_kwargs):
        return None

    monkeypatch.setattr(intake_service, "_workflow_enabled", enabled)
    ids = await _seed(audit_pg_factory, status="submitted")
    async with audit_pg_factory() as db:
        request = await db.get(IntakeRequest, ids["request"])
        patient = await db.get(Patient, ids["patient"])
        baseline = await intake_service.anamnese_fingerprint(db, patient.id)

    async def write_anamnese():
        async with audit_pg_factory() as db:
            await db.execute(select(Patient.id).where(Patient.id == ids["patient"]).with_for_update())
            await anamnese_service.upsert_entries(db, patient_id=ids["patient"], entries=[AnamneseEntryInput(section="Profissional", value="Mudou")])
            await asyncio.sleep(0.2)
            await db.commit()

    async def import_intake():
        await asyncio.sleep(0.05)
        async with audit_pg_factory() as db:
            request = await db.get(IntakeRequest, ids["request"])
            actor = await db.get(Professional, ids["professional"])
            try:
                await intake_service.review(db, request=request, actor=actor, body=IntakeReview(expected_version=1, anamnese_fingerprint=baseline, selected_fields=["reasonForReferral"], command_key="review-1"))
            except HTTPException as exc:
                await db.rollback()
                return exc.status_code
            await db.commit()
            return "reviewed"

    result = await asyncio.gather(write_anamnese(), import_intake())
    assert result[1] == 409


@pytest.mark.asyncio
async def test_stale_anamnese_writer_cannot_update_after_completion(audit_pg_factory):
    ids = await _seed(audit_pg_factory)
    async with audit_pg_factory() as db:
        db.add(AnamneseEntry(patient_id=ids["patient"], section="Inicial", value="Conteúdo"))
        await db.commit()

    checked = asyncio.Event()
    release = asyncio.Event()

    async def stale_writer():
        async with audit_pg_factory() as db:
            patient = await db.get(Patient, ids["patient"])
            anamnese_service.assert_editable(patient)
            checked.set()
            await release.wait()
            try:
                await anamnese_service.upsert_entries(
                    db,
                    patient_id=ids["patient"],
                    entries=[AnamneseEntryInput(section="Depois", value="Não deve gravar")],
                )
                await db.commit()
                return "saved"
            except HTTPException as exc:
                await db.rollback()
                return exc.status_code

    writer = asyncio.create_task(stale_writer())
    await asyncio.wait_for(checked.wait(), timeout=5)
    async with audit_pg_factory() as db:
        patient = await db.get(Patient, ids["patient"])
        await anamnese_service.complete_anamnese(db, patient=patient, entries=None)
        await db.commit()
    release.set()

    assert await asyncio.wait_for(writer, timeout=10) == 409
    async with audit_pg_factory() as db:
        patient = await db.get(Patient, ids["patient"])
        assert patient.anamnese_status == "completed"
        assert await db.scalar(
            select(func.count()).select_from(AnamneseEntry).where(
                AnamneseEntry.patient_id == ids["patient"], AnamneseEntry.section == "Depois"
            )
        ) == 0


@pytest.mark.asyncio
async def test_intake_file_incorporation_retry_is_single_attachment(audit_pg_factory, monkeypatch):
    async def enabled(*_args, **_kwargs):
        return None

    monkeypatch.setattr(intake_service, "_workflow_enabled", enabled)
    ids = await _seed(audit_pg_factory, status="submitted")
    async with audit_pg_factory() as db:
        item = IntakeFile(intake_request_id=ids["request"], caregiver_id=ids["caregiver"], name="laudo.pdf", content_type="application/pdf", size_bytes=8, storage_key=f"intake/{uuid4()}/laudo.pdf")
        db.add(item)
        await db.commit()

    body = IntakeReview(expected_version=1, selected_file_ids=[item.id], command_key="review-files")
    async with audit_pg_factory() as db:
        request = await db.get(IntakeRequest, ids["request"])
        actor = await db.get(Professional, ids["professional"])
        await intake_service.review(db, request=request, actor=actor, body=body)
        await db.commit()
        request = await db.get(IntakeRequest, ids["request"])
        await intake_service.review(db, request=request, actor=actor, body=body)
        await db.commit()
        total = await db.scalar(select(func.count()).select_from(Attachment).where(Attachment.patient_id == ids["patient"]))
        assert total == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("intervening_action", ["revoke", "submit"])
async def test_intake_upload_revalidates_after_storage_before_association(audit_pg_factory, monkeypatch, intervening_action):
    async def enabled(*_args, **_kwargs):
        return None

    monkeypatch.setattr(intake_service, "_workflow_enabled", enabled)
    ids = await _seed(audit_pg_factory)
    raw_token = f"pg-token-{uuid4().hex}"
    async with audit_pg_factory() as db:
        db.add(IntakeGrant(intake_request_id=ids["request"], caregiver_id=ids["caregiver"], token_hash=hash_token(raw_token), expires_at=datetime.now(UTC) + timedelta(days=1), authorization={"reviewed": True}, created_by_professional_id=ids["professional"]))
        await db.commit()

    started = asyncio.Event()
    release = asyncio.Event()

    async def paused_upload(*_args, **_kwargs):
        started.set()
        await release.wait()

    monkeypatch.setattr(intake_service.storage_service, "upload", paused_upload)

    async def upload():
        async with audit_pg_factory() as db:
            context = await intake_service.resolve_public(db, raw_token, lock=True)
            try:
                await intake_service.upload_file(db, ctx=context, raw_token=raw_token, upload=UploadFile(file=BytesIO(b"%PDF-1.4 pg"), filename="x.pdf", headers={"content-type": "application/pdf"}))
                await db.commit()
                return "uploaded"
            except HTTPException as exc:
                await db.rollback()
                return exc.status_code

    task = asyncio.create_task(upload())
    await asyncio.wait_for(started.wait(), timeout=5)
    async with audit_pg_factory() as db:
        if intervening_action == "revoke":
            grant = await db.scalar(select(IntakeGrant).where(IntakeGrant.intake_request_id == ids["request"]))
            grant.revoked_at = datetime.now(UTC)
        else:
            request = await db.get(IntakeRequest, ids["request"])
            request.status = "submitted"
            request.version = 2
            request.submitted_at = datetime.now(UTC)
        await db.commit()
    release.set()
    assert await asyncio.wait_for(task, timeout=10) == 409 + (0 if intervening_action == "submit" else 1)
    async with audit_pg_factory() as db:
        assert await db.scalar(select(func.count()).select_from(IntakeFile).where(IntakeFile.intake_request_id == ids["request"])) == 0
