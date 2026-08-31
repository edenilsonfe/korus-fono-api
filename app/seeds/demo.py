"""Development seed — creates demo professional and sample patients."""

import asyncio
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select

from app.core.constants import AVATAR_COLORS, CLINICAL_DOMAIN_CATALOG
from app.core.security import hash_password
from app.db.session import AsyncSessionLocal, engine
from app.models.app_notification import AppNotification
from app.models.assessment import Assessment, ProtocolCatalog
from app.models.caregiver import Caregiver
from app.models.goal import ClinicalDomainSnapshot, Goal
from app.models.patient import Patient
from app.models.professional import Professional
from app.seeds.protocols import PROTOCOLS
from app.services.timeline import create_timeline_event


async def seed_protocols(session) -> None:
    active_ids = {p["id"] for p in PROTOCOLS}
    for p in PROTOCOLS:
        existing = await session.get(ProtocolCatalog, p["id"])
        if existing:
            existing.name = p["name"]
            existing.full_name = p["full_name"]
            existing.description = p["description"]
            existing.age_range = p["age_range"]
            existing.field_templates = p["field_templates"]
        else:
            session.add(ProtocolCatalog(**p))

    result = await session.execute(select(ProtocolCatalog))
    for existing in result.scalars().all():
        if existing.id in active_ids:
            continue
        has_assessments = await session.execute(
            select(Assessment.id).where(Assessment.protocol_id == existing.id).limit(1)
        )
        if has_assessments.scalar_one_or_none() is None:
            await session.delete(existing)
        else:
            # Keep row for FK/history; hide from applicable catalog.
            existing.is_active = False


async def seed_demo(session) -> None:
    result = await session.execute(
        select(Professional).where(Professional.email == "admin@admin.com")
    )
    existing = result.scalar_one_or_none()
    if existing is not None:
        # ponytail: demo local must stay usable when email verification is on
        if existing.email_verified_at is None:
            existing.email_verified_at = datetime.now(UTC)
            await session.flush()
        return

    professional = Professional(
        email="admin@admin.com",
        password_hash=hash_password("admin123"),
        name="Dra. Camila Rocha",
        specialty_key="fono",
        specialty="Fonoaudiologia",
        council="CRFa 2-12345",
        phone="(11) 98888-1010",
        avatar_color="oklch(0.58 0.12 205)",
        is_staff=True,
        subscription_status="active",
        email_verified_at=datetime.now(UTC),
    )
    session.add(professional)
    await session.flush()

    names = [
        ("João Silva", ["tea"], "ativo"),
        ("Maria Oliveira", ["linguagem"], "ativo"),
        ("Pedro Santos", ["apraxia"], "avaliacao"),
        ("Ana Souza", ["dislexia"], "ativo"),
        ("Lucas Costa", ["outros"], "pausado"),
    ]
    for i, (name, diags, status) in enumerate(names):
        birth = date.today().replace(year=date.today().year - (5 + i))
        patient = Patient(
            professional_id=professional.id,
            name=name,
            birth_date=birth,
            diagnosis_keys=diags,
            status=status,
            start_date=date.today() - timedelta(days=90 + i * 10),
            avatar_color=AVATAR_COLORS[i % len(AVATAR_COLORS)],
        )
        session.add(patient)
        await session.flush()
        session.add(
            Caregiver(
                patient_id=patient.id,
                name=f"Responsável {name.split()[0]}",
                relation="Mãe",
                phone="(11) 98888-1010",
                is_primary=True,
            )
        )
        session.add(
            Goal(
                patient_id=patient.id,
                professional_id=professional.id,
                title="Aumentar vocabulário expressivo",
                area="Linguagem",
                progress=60 + i * 5,
                start_date=date.today() - timedelta(days=30),
                status="Em andamento",
            )
        )
        for j, domain in enumerate(CLINICAL_DOMAIN_CATALOG[:4]):
            for k in range(4):
                session.add(
                    ClinicalDomainSnapshot(
                        patient_id=patient.id,
                        key=domain["key"],
                        label=domain["label"],
                        score=40 + i * 3 + j * 5 + k * 4,
                        recorded_at=date.today() - timedelta(days=30 - k * 7),
                    )
                )
        await create_timeline_event(
            session,
            patient_id=patient.id,
            professional_id=professional.id,
            event_type="meta",
            title="Paciente cadastrado",
            description="Início do acompanhamento na clínica",
        )


async def seed_demo_announcements(session) -> None:
    """Seed 3 demo broadcast announcements (dev only)."""
    existing = await session.execute(
        select(AppNotification).where(AppNotification.kind == "broadcast").limit(1)
    )
    if existing.scalar_one_or_none():
        return

    demos = [
        AppNotification(
            kind="broadcast",
            type="feature",
            title="Novo módulo de relatórios com IA",
            body=(
                "Agora você pode gerar relatórios clínicos, escolares e evolutivos "
                "direto do prontuário com um clique.\n\nAcesse em Relatórios IA."
            ),
            deep_link="/relatorios",
            severity="info",
            audience="all",
            status="published",
        ),
        AppNotification(
            kind="broadcast",
            type="notice",
            title="Manutenção programada",
            body=(
                "No sábado 03/08 das 02h às 04h a plataforma passará por uma "
                "manutenção e ficará indisponível por breve período."
            ),
            severity="warning",
            audience="all",
            status="published",
        ),
        AppNotification(
            kind="broadcast",
            type="tutorial",
            title="Conecte seu WhatsApp",
            body=(
                "Envie lembretes e confirmações de consulta automaticamente. "
                "Conecte seu número em poucos cliques."
            ),
            deep_link="/whatsapp",
            severity="info",
            audience="all",
            status="published",
        ),
    ]
    for ann in demos:
        session.add(ann)


async def run_seed() -> None:
    async with AsyncSessionLocal() as session:
        from app.services.plan_catalog_seed import seed_plan_catalog

        await seed_protocols(session)
        await seed_plan_catalog(session)
        await seed_demo(session)
        await seed_demo_announcements(session)
        from app.seeds.resources import seed_resources

        await seed_resources(session)
        await session.commit()
    await engine.dispose()
    print("Seed concluído.")


if __name__ == "__main__":
    asyncio.run(run_seed())
