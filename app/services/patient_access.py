from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.care_team import PatientCareTeamMember
from app.models.patient import Patient
from app.models.professional import Professional
from app.services.feature_flag_service import FeatureFlagService

FEATURE_KEY = "multidisciplinary_aba"

ROLE_PERMISSIONS = {
    "coordinator": frozenset(
        {
            "care_team:read",
            "care_team:manage",
            "clinical:read",
            "clinical:write",
            "contacts:read",
            "patient:write",
            "patient:delete",
            "therapy_plan:write",
            "program:manage",
            "program:collect",
            "measurement:review",
        }
    ),
    "supervisor": frozenset(
        {
            "care_team:read",
            "clinical:read",
            "clinical:write",
            "program:manage",
            "program:collect",
            "measurement:review",
        }
    ),
    "practitioner": frozenset(
        {"care_team:read", "clinical:read", "clinical:write", "program:collect"}
    ),
}


@dataclass(frozen=True)
class PatientAccess:
    patient: Patient
    role: str
    is_owner: bool
    permissions: frozenset[str]


async def _feature_is_enabled(db: AsyncSession, patient: Patient) -> bool:
    owner = await db.get(Professional, patient.professional_id)
    return bool(
        owner
        and not owner.is_disabled
        and await FeatureFlagService(db).is_enabled(owner, FEATURE_KEY)
    )


async def resolve_patient_access(
    db: AsyncSession,
    patient_id: UUID,
    actor: Professional,
) -> PatientAccess | None:
    patient = await db.get(Patient, patient_id)
    if patient is None or not await _feature_is_enabled(db, patient):
        return None
    if patient.professional_id == actor.id:
        return PatientAccess(
            patient, "coordinator", True, ROLE_PERMISSIONS["coordinator"]
        )

    member = await db.scalar(
        select(PatientCareTeamMember).where(
            PatientCareTeamMember.patient_id == patient.id,
            PatientCareTeamMember.professional_id == actor.id,
            PatientCareTeamMember.status == "active",
        )
    )
    if member is None:
        return None
    return PatientAccess(patient, member.role, False, ROLE_PERMISSIONS[member.role])


async def resolve_clinical_patient_access(
    db: AsyncSession,
    patient_id: UUID,
    actor: Professional,
) -> PatientAccess | None:
    """Keep ordinary owner access independent from the rollout flag."""
    patient = await db.get(Patient, patient_id)
    if patient is None:
        return None
    if patient.professional_id == actor.id:
        return PatientAccess(
            patient, "coordinator", True, ROLE_PERMISSIONS["coordinator"]
        )
    return await resolve_patient_access(db, patient_id, actor)


async def list_shared_patient_accesses(
    db: AsyncSession,
    actor: Professional,
) -> dict[UUID, PatientAccess]:
    rows = (
        await db.execute(
            select(PatientCareTeamMember, Patient, Professional)
            .join(Patient, Patient.id == PatientCareTeamMember.patient_id)
            .join(Professional, Professional.id == Patient.professional_id)
            .where(
                PatientCareTeamMember.professional_id == actor.id,
                PatientCareTeamMember.status == "active",
                Professional.is_disabled.is_(False),
            )
        )
    ).all()
    result: dict[UUID, PatientAccess] = {}
    for member, patient, owner in rows:
        if await FeatureFlagService(db).is_enabled(owner, FEATURE_KEY):
            result[patient.id] = PatientAccess(
                patient, member.role, False, ROLE_PERMISSIONS[member.role]
            )
    return result


async def list_accessible_patient_ids(
    db: AsyncSession,
    actor: Professional,
) -> set[UUID]:
    own_ids = set(
        (
            await db.execute(
                select(Patient.id).where(Patient.professional_id == actor.id)
            )
        ).scalars()
    )
    own_ids.update(await list_shared_patient_accesses(db, actor))
    return own_ids


def has_permission(access: PatientAccess, permission: str) -> bool:
    return permission in access.permissions
