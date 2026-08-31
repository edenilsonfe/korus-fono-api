"""EAT-10 / MASA removed from applicable catalog; history kept via inactive seed rows."""

from datetime import date

from sqlalchemy import select

from app.core.instrument_aliases import (
    CLIENT_SCORED_PROTOCOLS,
    PROTOCOL_TO_INSTRUMENT_SLUG,
    resolve_instrument_slug,
)
from app.models.assessment import Assessment, ProtocolCatalog
from app.seeds.demo import seed_protocols
from app.seeds.protocols import PROTOCOLS


REMOVED_FROM_CATALOG = ("eat10", "masa", "tli", "spm", "tvip", "sdq")
DISABLED_BY_DEFAULT = ("abfw", "amiofe")


def test_protocols_seed_excludes_discontinued():
    ids = {p["id"] for p in PROTOCOLS}
    for pid in REMOVED_FROM_CATALOG:
        assert pid not in ids


def test_aliases_exclude_discontinued_manifest_slugs():
    assert "eat10" not in PROTOCOL_TO_INSTRUMENT_SLUG
    assert "masa" not in PROTOCOL_TO_INSTRUMENT_SLUG
    assert "tli" not in PROTOCOL_TO_INSTRUMENT_SLUG
    assert "tvip" not in PROTOCOL_TO_INSTRUMENT_SLUG
    assert resolve_instrument_slug("eat10") is None
    assert resolve_instrument_slug("masa") is None
    assert resolve_instrument_slug("tli") is None
    assert resolve_instrument_slug("tvip") is None
    assert "sdq" not in CLIENT_SCORED_PROTOCOLS


async def test_seed_keeps_default_disabled_protocols_disabled(db_session):
    protocols = []
    for protocol_id in DISABLED_BY_DEFAULT:
        protocol = await db_session.get(ProtocolCatalog, protocol_id)
        assert protocol is not None
        protocol.is_active = False
        protocols.append(protocol)
    await db_session.commit()

    await seed_protocols(db_session)
    await db_session.commit()

    for protocol in protocols:
        await db_session.refresh(protocol)
        assert protocol.is_active is False


async def test_seed_creates_default_disabled_protocols_inactive(db_session):
    for protocol_id in DISABLED_BY_DEFAULT:
        protocol = await db_session.get(ProtocolCatalog, protocol_id)
        assert protocol is not None
        await db_session.delete(protocol)
    await db_session.commit()

    await seed_protocols(db_session)
    await db_session.commit()

    for protocol_id in DISABLED_BY_DEFAULT:
        protocol = await db_session.get(ProtocolCatalog, protocol_id)
        assert protocol is not None
        assert protocol.is_active is False


async def test_seed_deactivates_orphans_with_assessments(db_session, professional, patient):
    db_session.add(
        ProtocolCatalog(
            id="eat10",
            name="EAT-10",
            full_name="Eating Assessment Tool — 10 itens",
            description="legado",
            age_range="Adultos",
            field_templates=[],
            is_active=True,
        )
    )
    await db_session.flush()
    db_session.add(
        Assessment(
            patient_id=patient.id,
            professional_id=professional.id,
            protocol_id="eat10",
            date=date.today(),
            result="Histórico",
            percentage=40,
            interpretation="",
            fields=[],
            answers={},
            status="completed",
        )
    )
    await db_session.commit()

    await seed_protocols(db_session)
    await db_session.commit()

    orphan = await db_session.get(ProtocolCatalog, "eat10")
    assert orphan is not None
    assert orphan.is_active is False

    active_ids = {
        row.id
        for row in (
            await db_session.execute(select(ProtocolCatalog).where(ProtocolCatalog.is_active.is_(True)))
        )
        .scalars()
        .all()
    }
    for pid in REMOVED_FROM_CATALOG:
        assert pid not in active_ids
