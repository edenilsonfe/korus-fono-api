from sqlalchemy import select

from app.core.utils import utcnow
from app.models.caregiver import Caregiver
from app.models.intake import IntakeRequest, IntakeFile
from app.services.storage_cleanup_service import storage_key_in_use


async def test_unreviewed_intake_file_protects_blob_until_removed(db_session, patient, professional):
    caregiver = await db_session.scalar(select(Caregiver).where(Caregiver.patient_id == patient.id))
    request = IntakeRequest(patient_id=patient.id, caregiver_id=caregiver.id, caregiver_name_snapshot=caregiver.name, owner_professional_id=professional.id)
    db_session.add(request)
    await db_session.flush()
    file = IntakeFile(intake_request_id=request.id, caregiver_id=caregiver.id, name="relato.pdf", content_type="application/pdf", size_bytes=100, storage_key="patients/test/intake/file.pdf")
    db_session.add(file)
    await db_session.flush()
    assert await storage_key_in_use(db_session, file.storage_key)
    file.deleted_at = utcnow()
    await db_session.flush()
    assert not await storage_key_in_use(db_session, file.storage_key)
