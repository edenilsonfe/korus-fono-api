"""F14 onda 1/3 — corridas reais do acesso, grants e arquivos (gate §5.3).

Exigem ``TEST_AUDIT_PG_URL`` apontando para o banco descartável ``korus_audit``
(fixture ``audit_pg_factory``); sem a variável ficam skipped, o que NÃO aprova o
gate — o integrador roda com o banco disponível (``.run_audit_gate.sh``).
SQLite não prova ``FOR UPDATE`` nem o índice parcial único sob concorrência.

Corridas cobertas:
- emissão dupla do mesmo destinatário: um 201 e o outro 409 (nunca dois links
  vivos nem um sucesso imediatamente perdido);
- primeira criação concorrente do portal: uma linha, um evento;
- desativação durante a emissão: nenhum link sobrevive vivo ao disable;
- onda 2: duas publicações do mesmo item → UMA revisão; publicação versus
  retirada do item → nada fica público; retirada do responsável versus
  publicação → destinatário retirado nunca termina com audiência viva;
- onda 3: publicação de material versus revogação da licença e publicação de
  relatório versus revogação da entrega F1 — em QUALQUER ordem o arquivo não
  é servido depois; leitura de arquivo revalida pós-I/O com commit REAL de
  outra sessão (revogação no meio do download/render → 409 sem bytes).
"""

import asyncio
import hashlib
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.models.ai import AIReport
from app.models.caregiver import Caregiver
from app.models.family_portal import (
    FamilyPortal,
    FamilyPortalEvent,
    FamilyPortalGrant,
    FamilyPortalRecipient,
)
from app.models.family_portal_content import (
    FamilyPortalItem,
    FamilyPortalItemAudience,
    FamilyPortalItemRevision,
)
from app.models.patient import Patient
from app.models.professional import Professional
from app.models.report_delivery import ReportDelivery
from app.models.resource import Resource
from app.models.resource_license import ResourceLicense
from app.models.session import Session
from app.schemas.family_portal import (
    FamilyPortalEnableRequest,
    FamilyPortalGrantIssueRequest,
    FamilyPortalWithdrawRequest,
)
from app.schemas.family_portal_content import FamilyPortalItemPublishRequest
from app.services import (
    family_portal_access,
    family_portal_content,
    family_portal_files,
)
from app.services.report_delivery_service import (
    build_delivery_snapshot,
    delivery_content_hash,
)

MATERIAL_BYTES = b"%PDF-1.4 corrida-do-material"
REPORT_TEXT = "## Sessao\nTexto congelado da corrida."


async def _seed_patient(factory, *, with_portal: bool) -> dict:
    async with factory() as db:
        professional = Professional(
            email=f"race-{uuid4().hex}@example.com",
            name="Race",
            password_hash="unused",
        )
        db.add(professional)
        await db.flush()
        patient = Patient(
            professional_id=professional.id,
            name="Synthetic",
            birth_date=date(2020, 1, 1),
            start_date=date.today(),
            avatar_color="teal",
            diagnosis_keys=[],
            status="ativo",
        )
        db.add(patient)
        await db.flush()
        caregiver = Caregiver(
            patient_id=patient.id, name="Maria", relation="Mãe", is_primary=True
        )
        db.add(caregiver)
        await db.flush()
        ids = {
            "professional_id": professional.id,
            "patient_id": patient.id,
            "caregiver_id": caregiver.id,
        }
        if with_portal:
            portal = FamilyPortal(
                patient_id=patient.id,
                owner_professional_id=professional.id,
                enabled=True,
                access_version=0,
                version=1,
            )
            db.add(portal)
            await db.flush()
            recipient = FamilyPortalRecipient(
                portal_id=portal.id,
                caregiver_id=caregiver.id,
                active=True,
                appointments_enabled=False,
                authorization_version=1,
                version=1,
            )
            db.add(recipient)
            await db.flush()
            db.add(
                FamilyPortalEvent(
                    portal_id=portal.id,
                    recipient_id=recipient.id,
                    actor_professional_id=professional.id,
                    event_type="recipient_authorized",
                    authorization_version=1,
                    payload={
                        "authorizedAt": (
                            datetime.now(UTC) - timedelta(days=1)
                        ).isoformat(),
                        "reference": "Termo",
                        "reviewed": True,
                    },
                    occurred_at=datetime.now(UTC),
                )
            )
            ids["portal_id"] = portal.id
            ids["recipient_id"] = recipient.id
        await db.commit()
        return ids


async def test_concurrent_grant_issuance_yields_a_single_success(
    audit_pg_factory,
):
    factory = audit_pg_factory
    ids = await _seed_patient(factory, with_portal=True)
    body = FamilyPortalGrantIssueRequest(
        expires_in_days=30,
        expected_recipient_version=1,
        rotate_from_grant_id=None,
    )

    async def issue():
        async with factory() as db:
            actor = await db.get(Professional, ids["professional_id"])
            try:
                grant, _url = await family_portal_access.issue_grant(
                    db,
                    ids["patient_id"],
                    ids["recipient_id"],
                    actor,
                    body,
                )
            except HTTPException as exc:
                await db.rollback()
                return ("rejected", exc.status_code)
            await db.commit()
            return ("issued", grant.id)

    first, second = await asyncio.wait_for(
        asyncio.gather(issue(), issue()), timeout=20
    )
    assert sorted([first[0], second[0]]) == ["issued", "rejected"]
    rejected = next(
        value for kind, value in (first, second) if kind == "rejected"
    )
    assert rejected == 409

    async with factory() as db:
        grants = (await db.execute(select(FamilyPortalGrant))).scalars().all()
        issued_events = await db.scalar(
            select(func.count())
            .select_from(FamilyPortalEvent)
            .where(FamilyPortalEvent.event_type == "grant_issued")
        )
    assert len(grants) == 1
    assert grants[0].revoked_at is None  # o vencedor segue vivo
    assert issued_events == 1  # o perdedor não deixou trilha


async def test_concurrent_first_portal_creation_keeps_single_row(
    audit_pg_factory,
):
    factory = audit_pg_factory
    ids = await _seed_patient(factory, with_portal=False)
    body = FamilyPortalEnableRequest(enabled=True, expected_version=1)

    async def enable():
        async with factory() as db:
            actor = await db.get(Professional, ids["professional_id"])
            result = await family_portal_access.enable_portal(
                db, ids["patient_id"], actor, body
            )
            await db.commit()
            return result

    first, second = await asyncio.wait_for(
        asyncio.gather(enable(), enable()), timeout=20
    )
    assert first.enabled is True and second.enabled is True
    assert first.version == second.version == 1

    async with factory() as db:
        portals = (await db.execute(select(FamilyPortal))).scalars().all()
        enabled_events = await db.scalar(
            select(func.count())
            .select_from(FamilyPortalEvent)
            .where(FamilyPortalEvent.event_type == "portal_enabled")
        )
    assert len(portals) == 1
    assert portals[0].enabled is True
    assert portals[0].access_version == 0
    assert enabled_events == 1  # a segunda ativação foi idempotente


async def test_disable_during_issuance_never_leaves_a_live_grant(
    audit_pg_factory,
):
    factory = audit_pg_factory
    ids = await _seed_patient(factory, with_portal=True)
    body = FamilyPortalGrantIssueRequest(
        expires_in_days=30,
        expected_recipient_version=1,
        rotate_from_grant_id=None,
    )

    async def issue():
        async with factory() as db:
            actor = await db.get(Professional, ids["professional_id"])
            try:
                grant, _url = await family_portal_access.issue_grant(
                    db,
                    ids["patient_id"],
                    ids["recipient_id"],
                    actor,
                    body,
                )
            except HTTPException as exc:
                await db.rollback()
                return ("rejected", exc.status_code)
            await db.commit()
            return ("issued", grant.id)

    async def disable():
        async with factory() as db:
            actor = await db.get(Professional, ids["professional_id"])
            result = await family_portal_access.disable_portal(
                db, ids["patient_id"], actor
            )
            await db.commit()
            # Etiquetado como os demais: o gerador de desfechos desempacota
            # TODOS os resultados (kind, value) antes de filtrar.
            return ("disabled", result)

    outcomes = await asyncio.wait_for(
        asyncio.gather(issue(), disable()), timeout=20
    )
    issued = next(
        (value for kind, value in outcomes if kind == "issued"), None
    )
    rejected = next(
        (value for kind, value in outcomes if kind == "rejected"), None
    )
    # Ou a emissão venceu (e o disable a revogou) ou o disable venceu (e a
    # emissão foi recusada); nunca as duas coisas com link vivo no final.
    assert issued is not None or rejected == 409

    async with factory() as db:
        live = await db.scalar(
            select(func.count())
            .select_from(FamilyPortalGrant)
            .where(FamilyPortalGrant.revoked_at.is_(None))
        )
        portal = await db.scalar(select(FamilyPortal))
    assert live == 0
    assert portal.enabled is False
    assert portal.access_version == 1


# --------------------------------------------------------------------------- #
# Onda 2 — corridas da camada editorial (publicação/revisão/retirada de público)
# --------------------------------------------------------------------------- #


async def _seed_draft_item(factory, *, published: bool = False) -> dict:
    """Semeia sessão + item editorial (ou já publicado com revisão/público)."""
    ids = await _seed_patient(factory, with_portal=True)
    async with factory() as db:
        session = Session(
            patient_id=ids["patient_id"],
            professional_id=ids["professional_id"],
            date=datetime.now(UTC) - timedelta(days=1),
            duration=50,
            type="Sessão sintética",
            objectives=[],
            notes="notas privadas",
        )
        db.add(session)
        await db.flush()
        fingerprint = family_portal_content.session_source_fingerprint(session)
        now = datetime.now(UTC)
        draft_content = {"title": "Resumo", "body": "Texto aprovado."}
        item = FamilyPortalItem(
            portal_id=ids["portal_id"],
            kind="session_summary",
            status="draft",
            version=1,
            published_version=None,
            draft_content=draft_content,
            draft_recipient_ids=[str(ids["recipient_id"])],
            draft_source_fingerprint=fingerprint,
            session_id=session.id,
            created_by_professional_id=ids["professional_id"],
            created_at=now,
            updated_at=now,
        )
        db.add(item)
        await db.flush()
        if published:
            db.add(
                FamilyPortalItemRevision(
                    item_id=item.id,
                    version=1,
                    content=draft_content,
                    recipient_ids=[str(ids["recipient_id"])],
                    source_fingerprint=fingerprint,
                    source_metadata={
                        "sessionId": str(session.id),
                        "sessionDate": session.date.date().isoformat(),
                        "evolutionId": None,
                    },
                    published_by_professional_id=ids["professional_id"],
                    published_at=now,
                    expires_at=None,
                )
            )
            db.add(
                FamilyPortalItemAudience(
                    item_id=item.id, recipient_id=ids["recipient_id"]
                )
            )
            item.status = "published"
            item.published_version = 1
            item.published_at = now
        await db.commit()
        ids["session_id"] = session.id
        ids["item_id"] = item.id
        ids["fingerprint"] = fingerprint
        return ids


async def _live_public_item_count(factory, item_id) -> int:
    async with factory() as db:
        return int(
            await db.scalar(
                select(func.count())
                .select_from(FamilyPortalItem)
                .join(
                    FamilyPortalItemAudience,
                    FamilyPortalItemAudience.item_id == FamilyPortalItem.id,
                )
                .where(
                    FamilyPortalItem.id == item_id,
                    FamilyPortalItem.status == "published",
                )
            )
            or 0
        )


async def test_concurrent_publishes_create_a_single_revision(
    audit_pg_factory,
):
    """Duas publicações do mesmo item: UMA revisão, UMA audiência, nunca dois
    \"sucessos\" de revisões distintas."""
    factory = audit_pg_factory
    ids = await _seed_draft_item(factory)
    body = FamilyPortalItemPublishRequest(
        expected_version=1,
        expected_source_fingerprint=ids["fingerprint"],
        reviewed=True,
    )

    async def publish():
        async with factory() as db:
            actor = await db.get(Professional, ids["professional_id"])
            try:
                result = await family_portal_content.publish_item(
                    db, ids["patient_id"], actor, ids["item_id"], body
                )
            except HTTPException as exc:
                await db.rollback()
                return ("rejected", exc.status_code)
            await db.commit()
            return ("published", result.version)

    first, second = await asyncio.wait_for(
        asyncio.gather(publish(), publish()), timeout=20
    )
    outcomes = [first, second]
    assert any(kind == "published" for kind, _ in outcomes)
    for kind, value in outcomes:
        assert kind in ("published", "rejected")
        if kind == "rejected":
            assert value == 409
        else:
            assert value == 1  # a MESMA revisão v1 (no-op do perdedor)

    async with factory() as db:
        revisions = (
            (await db.execute(select(FamilyPortalItemRevision))).scalars().all()
        )
        audiences = (
            (await db.execute(select(FamilyPortalItemAudience))).scalars().all()
        )
        item = await db.get(FamilyPortalItem, ids["item_id"])
        await db.refresh(item)
    assert len(revisions) == 1 and revisions[0].version == 1
    assert len(audiences) == 1
    assert audiences[0].recipient_id == ids["recipient_id"]
    assert item.status == "published" and item.published_version == 1


async def test_concurrent_withdraw_versus_publish_keeps_nothing_public(
    audit_pg_factory,
):
    """Publicação concorrente com a retirada: no final NADA fica público e a
    publicação preparada antes da retirada não vence (409) ou vira no-op."""
    factory = audit_pg_factory
    ids = await _seed_draft_item(factory, published=True)
    body = FamilyPortalItemPublishRequest(
        expected_version=1,
        expected_source_fingerprint=ids["fingerprint"],
        reviewed=True,
    )

    async def publish():
        async with factory() as db:
            actor = await db.get(Professional, ids["professional_id"])
            try:
                result = await family_portal_content.publish_item(
                    db, ids["patient_id"], actor, ids["item_id"], body
                )
            except HTTPException as exc:
                await db.rollback()
                return ("rejected", exc.status_code)
            await db.commit()
            return ("published", result.version)

    async def withdraw():
        async with factory() as db:
            actor = await db.get(Professional, ids["professional_id"])
            result = await family_portal_content.withdraw_item(
                db, ids["patient_id"], actor, ids["item_id"]
            )
            await db.commit()
            return ("withdrawn", result.status)

    outcomes = await asyncio.wait_for(
        asyncio.gather(publish(), withdraw()), timeout=20
    )
    for kind, value in outcomes:
        if kind == "rejected":
            assert value == 409  # retirada versionou antes da publicação
        elif kind == "published":
            assert value == 1  # no-op sobre a revisão vigente
        else:
            assert value == "withdrawn"

    async with factory() as db:
        item = await db.get(FamilyPortalItem, ids["item_id"])
        await db.refresh(item)
        audiences = (
            (await db.execute(select(FamilyPortalItemAudience))).scalars().all()
        )
    assert item.status == "withdrawn"
    assert audiences == []
    assert await _live_public_item_count(factory, ids["item_id"]) == 0


async def test_concurrent_recipient_withdrawal_blocks_publication(
    audit_pg_factory,
):
    """Retirada do responsável concorrente com a publicação: o destinatário
    retirado NUNCA termina com audiência viva (e a publicação é recusada ou
    perde a audiência no strip)."""
    factory = audit_pg_factory
    ids = await _seed_draft_item(factory)
    body = FamilyPortalItemPublishRequest(
        expected_version=1,
        expected_source_fingerprint=ids["fingerprint"],
        reviewed=True,
    )

    async def publish():
        async with factory() as db:
            actor = await db.get(Professional, ids["professional_id"])
            try:
                result = await family_portal_content.publish_item(
                    db, ids["patient_id"], actor, ids["item_id"], body
                )
            except HTTPException as exc:
                await db.rollback()
                return ("rejected", exc.status_code)
            await db.commit()
            return ("published", result.version)

    async def withdraw_recipient():
        async with factory() as db:
            actor = await db.get(Professional, ids["professional_id"])
            result = await family_portal_access.withdraw_recipient(
                db,
                ids["patient_id"],
                ids["recipient_id"],
                actor,
                FamilyPortalWithdrawRequest(reason="professional_decision"),
            )
            await db.commit()
            return ("withdrawn", result.active)

    outcomes = await asyncio.wait_for(
        asyncio.gather(publish(), withdraw_recipient()), timeout=20
    )
    for kind, value in outcomes:
        if kind == "rejected":
            # Sem responsável ativo não há lista válida para publicar.
            assert value in (409, 422)
        elif kind == "published":
            assert value == 1
        else:
            assert value is False

    async with factory() as db:
        recipient = await db.get(FamilyPortalRecipient, ids["recipient_id"])
        await db.refresh(recipient)
        audiences = (
            (await db.execute(select(FamilyPortalItemAudience))).scalars().all()
        )
        item = await db.get(FamilyPortalItem, ids["item_id"])
        await db.refresh(item)
    assert recipient.active is False
    assert audiences == []
    assert await _live_public_item_count(factory, ids["item_id"]) == 0
    # O rascunho perdeu o destinatário retirado (strip), então não ficou uma
    # publicação pronta para um responsável sem autorização.
    assert [str(value) for value in (item.draft_recipient_ids or [])] == []


# --------------------------------------------------------------------------- #
# Onda 3 — corridas de arquivo: publicação × revogação e revalidação pós-I/O
# --------------------------------------------------------------------------- #


async def _issue_portal_token(factory, ids: dict) -> str:
    async with factory() as db:
        actor = await db.get(Professional, ids["professional_id"])
        _grant, url = await family_portal_access.issue_grant(
            db,
            ids["patient_id"],
            ids["recipient_id"],
            actor,
            FamilyPortalGrantIssueRequest(
                expires_in_days=30,
                expected_recipient_version=1,
                rotate_from_grant_id=None,
            ),
        )
        await db.commit()
        return url.split("#token=", 1)[1]


def _allow_material_storage(monkeypatch, storage_key: str) -> None:
    """Um único objeto no storage falso; o singleton é compartilhado."""

    async def fake_download(
        key: str, max_bytes: int, timeout_seconds: float = 30.0
    ) -> tuple[bytes, str | None]:
        assert key == storage_key
        return MATERIAL_BYTES, "application/pdf"

    monkeypatch.setattr(
        "app.services.family_portal_files.storage_service.download_limited",
        fake_download,
    )


async def _seed_material_item(factory, *, published: bool = False) -> dict:
    """Recurso + licença declarada familiar + item de material (rascunho ou publicado)."""
    ids = await _seed_patient(factory, with_portal=True)
    async with factory() as db:
        digest = hashlib.sha256(MATERIAL_BYTES).hexdigest()
        storage_key = f"resources/race/{uuid4().hex}.pdf"
        resource = Resource(
            owner_professional_id=ids["professional_id"],
            title="Material da corrida",
            description="",
            categories=["Linguagem"],
            format="PDF",
            file_size_bytes=len(MATERIAL_BYTES),
            author="Race",
            storage_key=storage_key,
            content_type="application/pdf",
            content_sha256=digest,
        )
        db.add(resource)
        await db.flush()
        license_row = ResourceLicense(
            resource_id=resource.id,
            version=1,
            status="declared",
            origin="original",
            rights_holder="Race",
            attribution="",
            allow_professional_distribution=True,
            allow_family_delivery=True,
            content_sha256=digest,
        )
        db.add(license_row)
        await db.flush()
        fingerprint = family_portal_content.material_source_fingerprint(
            resource, license_row
        )
        source_metadata = {
            "resourceId": str(resource.id),
            "licenseId": str(license_row.id),
            "licenseVersion": 1,
            "sha256": digest,
            "contentType": "application/pdf",
            "sizeBytes": len(MATERIAL_BYTES),
            "attribution": "",
        }
        now = datetime.now(UTC)
        item = FamilyPortalItem(
            portal_id=ids["portal_id"],
            kind="material",
            status="draft",
            version=1,
            published_version=None,
            draft_content={"title": "Material", "instructions": ""},
            draft_recipient_ids=[str(ids["recipient_id"])],
            draft_source_fingerprint=fingerprint,
            resource_id=resource.id,
            created_by_professional_id=ids["professional_id"],
            created_at=now,
            updated_at=now,
        )
        db.add(item)
        await db.flush()
        if published:
            db.add(
                FamilyPortalItemRevision(
                    item_id=item.id,
                    version=1,
                    content=dict(item.draft_content),
                    recipient_ids=[str(ids["recipient_id"])],
                    source_fingerprint=fingerprint,
                    source_metadata=source_metadata,
                    published_by_professional_id=ids["professional_id"],
                    published_at=now,
                    expires_at=None,
                )
            )
            db.add(
                FamilyPortalItemAudience(
                    item_id=item.id, recipient_id=ids["recipient_id"]
                )
            )
            item.status = "published"
            item.published_version = 1
            item.published_at = now
        await db.commit()
        ids.update(
            {
                "resource_id": resource.id,
                "license_id": license_row.id,
                "item_id": item.id,
                "fingerprint": fingerprint,
                "storage_key": storage_key,
            }
        )
        return ids


async def _seed_report_item(factory, *, published: bool = False) -> dict:
    """Relatório pais finalizado + entrega F1 com snapshot + item (rascunho/publicado)."""
    ids = await _seed_patient(factory, with_portal=True)
    async with factory() as db:
        professional = await db.get(Professional, ids["professional_id"])
        patient = await db.get(Patient, ids["patient_id"])
        report = AIReport(
            professional_id=professional.id,
            patient_id=patient.id,
            type="pais",
            date=date(2026, 9, 1),
            preview=REPORT_TEXT[:200],
            content=REPORT_TEXT,
            status="finalized",
        )
        db.add(report)
        await db.flush()
        delivery = ReportDelivery(
            report_id=report.id,
            professional_id=professional.id,
            patient_id=patient.id,
            channel="link",
            recipient_label="Link avulso",
            token_hash=uuid4().hex + uuid4().hex,
            expires_at=datetime.now(UTC) + timedelta(days=30),
            document_snapshot=build_delivery_snapshot(
                report=report, patient=patient, professional=professional
            ),
        )
        db.add(delivery)
        await db.flush()
        fingerprint = family_portal_content.report_source_fingerprint(delivery)
        source_metadata = {
            "deliveryId": str(delivery.id),
            "reportId": str(report.id),
            "reportVersion": 1,
            "contentHash": delivery_content_hash(delivery),
            "reportType": "pais",
        }
        now = datetime.now(UTC)
        item = FamilyPortalItem(
            portal_id=ids["portal_id"],
            kind="report",
            status="draft",
            version=1,
            published_version=None,
            draft_content={"title": "Relatório"},
            draft_recipient_ids=[str(ids["recipient_id"])],
            draft_source_fingerprint=fingerprint,
            delivery_id=delivery.id,
            created_by_professional_id=ids["professional_id"],
            created_at=now,
            updated_at=now,
        )
        db.add(item)
        await db.flush()
        if published:
            db.add(
                FamilyPortalItemRevision(
                    item_id=item.id,
                    version=1,
                    content=dict(item.draft_content),
                    recipient_ids=[str(ids["recipient_id"])],
                    source_fingerprint=fingerprint,
                    source_metadata=source_metadata,
                    published_by_professional_id=ids["professional_id"],
                    published_at=now,
                    expires_at=None,
                )
            )
            db.add(
                FamilyPortalItemAudience(
                    item_id=item.id, recipient_id=ids["recipient_id"]
                )
            )
            item.status = "published"
            item.published_version = 1
            item.published_at = now
        await db.commit()
        ids.update(
            {
                "report_id": report.id,
                "delivery_id": delivery.id,
                "item_id": item.id,
                "fingerprint": fingerprint,
            }
        )
        return ids


async def test_material_publish_versus_license_revocation_never_serves(
    audit_pg_factory, monkeypatch
):
    """Publicação de material × revogação da licença: em qualquer ordem, uma
    leitura de arquivo depois da corrida NUNCA devolve bytes."""
    factory = audit_pg_factory
    ids = await _seed_material_item(factory)
    _allow_material_storage(monkeypatch, ids["storage_key"])
    token = await _issue_portal_token(factory, ids)
    publish_body = FamilyPortalItemPublishRequest(
        expected_version=1,
        expected_source_fingerprint=ids["fingerprint"],
        reviewed=True,
    )

    async def publish():
        async with factory() as db:
            actor = await db.get(Professional, ids["professional_id"])
            try:
                result = await family_portal_content.publish_item(
                    db, ids["patient_id"], actor, ids["item_id"], publish_body
                )
            except HTTPException as exc:
                await db.rollback()
                return ("rejected", exc.status_code)
            await db.commit()
            return ("published", result.version)

    async def revoke_license():
        async with factory() as db:
            row = await db.get(ResourceLicense, ids["license_id"])
            row.status = "revoked"
            await db.commit()
            return ("revoked", True)

    outcomes = await asyncio.wait_for(
        asyncio.gather(publish(), revoke_license()), timeout=20
    )
    for kind, value in outcomes:
        assert kind in ("published", "rejected", "revoked")
        if kind == "rejected":
            assert value == 409  # sem licença vigente não há publicação
        elif kind == "published":
            assert value == 1

    async with factory() as db:
        item = await db.get(FamilyPortalItem, ids["item_id"])
        await db.refresh(item)
        if item.status == "published":
            # Publicou antes da revogação: o card continua autorizado, mas a
            # leitura pública (detalhe e arquivo) revalida e bloqueia.
            context = await family_portal_access.resolve_public_context(db, token)
            detail = await family_portal_content.get_published_item(
                db,
                portal=context.portal,
                recipient=context.recipient,
                item_id=ids["item_id"],
            )
            assert detail.available is False
        status_code: int | None = None
        try:
            await family_portal_files.load_public_item_file(
                db, raw_token=token, item_id=ids["item_id"]
            )
        except HTTPException as exc:
            status_code = exc.status_code
        await db.rollback()
    # Nunca 200: rascunho perdedor vira 404; publicado com licença revogada
    # vira 409 — em nenhuma ordem bytes são servidos.
    assert status_code in (404, 409)


async def test_report_publish_versus_delivery_revocation_never_serves(
    audit_pg_factory,
):
    """Publicação de relatório × revogação da entrega F1: nenhuma leitura
    devolve o texto congelado depois da corrida."""
    factory = audit_pg_factory
    ids = await _seed_report_item(factory)
    token = await _issue_portal_token(factory, ids)
    publish_body = FamilyPortalItemPublishRequest(
        expected_version=1,
        expected_source_fingerprint=ids["fingerprint"],
        reviewed=True,
    )

    async def publish():
        async with factory() as db:
            actor = await db.get(Professional, ids["professional_id"])
            try:
                result = await family_portal_content.publish_item(
                    db, ids["patient_id"], actor, ids["item_id"], publish_body
                )
            except HTTPException as exc:
                await db.rollback()
                return ("rejected", exc.status_code)
            await db.commit()
            return ("published", result.version)

    async def revoke_delivery():
        async with factory() as db:
            row = await db.get(ReportDelivery, ids["delivery_id"])
            row.revoked_at = datetime.now(UTC)
            await db.commit()
            return ("revoked", True)

    outcomes = await asyncio.wait_for(
        asyncio.gather(publish(), revoke_delivery()), timeout=20
    )
    for kind, value in outcomes:
        assert kind in ("published", "rejected", "revoked")
        if kind == "rejected":
            assert value == 409
        elif kind == "published":
            assert value == 1

    async with factory() as db:
        item = await db.get(FamilyPortalItem, ids["item_id"])
        await db.refresh(item)
        if item.status == "published":
            context = await family_portal_access.resolve_public_context(db, token)
            detail = await family_portal_content.get_published_item(
                db,
                portal=context.portal,
                recipient=context.recipient,
                item_id=ids["item_id"],
            )
            assert detail.available is False
            assert REPORT_TEXT not in detail.model_dump_json()
        status_code: int | None = None
        try:
            await family_portal_files.load_public_item_file(
                db, raw_token=token, item_id=ids["item_id"]
            )
        except HTTPException as exc:
            status_code = exc.status_code
        await db.rollback()
    assert status_code in (404, 409)


async def test_material_file_revalidates_after_io_with_real_commit(
    audit_pg_factory, monkeypatch
):
    """Revogação REAL (outra sessão, commit próprio) no meio do download:
    os bytes são descartados e a resposta é 409 neutro."""
    factory = audit_pg_factory
    ids = await _seed_material_item(factory, published=True)
    token = await _issue_portal_token(factory, ids)

    async def revoke_mid_download(
        key: str, max_bytes: int, timeout_seconds: float = 30.0
    ) -> tuple[bytes, str | None]:
        async with factory() as other:
            row = await other.get(ResourceLicense, ids["license_id"])
            row.status = "revoked"
            await other.commit()
        return MATERIAL_BYTES, "application/pdf"

    monkeypatch.setattr(
        "app.services.family_portal_files.storage_service.download_limited",
        revoke_mid_download,
    )
    async with factory() as db:
        with pytest.raises(HTTPException) as excinfo:
            await family_portal_files.load_public_item_file(
                db, raw_token=token, item_id=ids["item_id"]
            )
        await db.rollback()
    assert excinfo.value.status_code == 409


async def test_report_file_revalidates_after_io_with_real_commit(
    audit_pg_factory, monkeypatch
):
    """Revogação REAL da entrega durante o render: 409 sem payload clínico."""
    factory = audit_pg_factory
    ids = await _seed_report_item(factory, published=True)
    token = await _issue_portal_token(factory, ids)

    import app.services.family_portal_files as files_module

    async def render_with_revocation(fn, *args, **kwargs):
        async with factory() as other:
            row = await other.get(ReportDelivery, ids["delivery_id"])
            row.revoked_at = datetime.now(UTC)
            await other.commit()
        return fn(*args, **kwargs)

    monkeypatch.setattr(files_module, "run_in_threadpool", render_with_revocation)
    async with factory() as db:
        with pytest.raises(HTTPException) as excinfo:
            await family_portal_files.load_public_item_file(
                db, raw_token=token, item_id=ids["item_id"]
            )
        await db.rollback()
    assert excinfo.value.status_code == 409
