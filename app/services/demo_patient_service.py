from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.demo_patient import DEMO_AVATAR_COLOR, DEMO_PATIENT_NAME, demo_patient_birth_date
from app.models.anamnese import AnamneseEntry
from app.models.assessment import Assessment, ProtocolCatalog
from app.models.evolution import Evolution
from app.models.goal import ClinicalDomainSnapshot, Goal
from app.models.patient import Patient
from app.models.professional import Professional


DEMO_ANAMNESIS_ENTRIES: tuple[tuple[str, str], ...] = (
    (
        "Gestação",
        "Gestação acompanhada com pré-natal regular, sem intercorrências relevantes relatadas.",
    ),
    (
        "Parto",
        "Parto a termo, sem necessidade de internação neonatal ou suporte respiratório.",
    ),
    (
        "Desenvolvimento motor",
        "Sustentou a cabeça aos 3 meses, sentou sem apoio aos 7 meses e iniciou marcha aos 14 meses.",
    ),
    (
        "Desenvolvimento da linguagem",
        "Balbucio presente no primeiro ano. Usa gestos comunicativos e palavras isoladas, "
        "com repertório expressivo abaixo do esperado para a idade.",
    ),
    (
        "Histórico escolar",
        "Frequenta berçário em período parcial e participa das atividades com mediação dos educadores.",
    ),
    (
        "Comorbidades",
        "Sem comorbidades confirmadas. Acompanhamento pediátrico de rotina mantido.",
    ),
    (
        "Medicamentos",
        "Não faz uso contínuo de medicamentos.",
    ),
    (
        "Observações",
        "Dados clínicos fictícios para demonstração do prontuário e das ferramentas de relatório do KorusFono.",
    ),
)

DEMO_EVOLUTIONS: tuple[tuple[str, int, str, str], ...] = (
    (
        "initial",
        150,
        "Avaliação inicial",
        "Paciente apresentou boa interação com o ambiente e intenção comunicativa por gestos. "
        "O repertório expressivo observado era de aproximadamente oito palavras, com compreensão "
        "de ordens simples apoiadas pelo contexto.",
    ),
    (
        "adaptation",
        110,
        "Adaptação ao processo terapêutico",
        "Adaptou-se à rotina das sessões e passou a sustentar atenção compartilhada por mais tempo. "
        "Foram trabalhadas imitação de ações, turnos comunicativos e nomeação de objetos familiares.",
    ),
    (
        "functional-communication",
        70,
        "Ampliação da comunicação funcional",
        "Aumentou o uso espontâneo de palavras para pedir ajuda, chamar familiares e recusar. "
        "Iniciou combinações de duas palavras com apoio de modelagem durante brincadeiras dirigidas.",
    ),
    (
        "recent",
        30,
        "Evolução recente",
        "Mantém participação consistente e maior iniciativa comunicativa. O vocabulário funcional "
        "está mais variado, com combinações simples em diferentes contextos e melhor compreensão "
        "de instruções de duas etapas.",
    ),
)

DEMO_ASSESSMENTS: tuple[dict, ...] = (
    {
        "key": "development-screening",
        "protocol_id": "desenvolvimento-infantil",
        "days_ago": 140,
        "result": "Atenção para linguagem expressiva",
        "percentage": 58,
        "interpretation": (
            "Marcos motores e sociais compatíveis com a faixa etária, com necessidade de "
            "estimulação e acompanhamento dos marcos de linguagem expressiva."
        ),
        "fields": [
            {"label": "Faixa etária aplicada", "value": "18–24 meses"},
            {"label": "Marcos não atingidos", "value": "Combinar palavras espontaneamente"},
            {"label": "Conduta", "value": "Estimulação de comunicação funcional"},
        ],
        "answers": {
            "ageBand": "18-24m",
            "areasObserved": ["linguagem", "socialização", "motor"],
        },
        "scores": {
            "domains": {
                "linguagem": {
                    "title": "Linguagem expressiva",
                    "percentage": 48,
                    "level": "em desenvolvimento",
                },
                "socializacao": {
                    "title": "Socialização",
                    "percentage": 68,
                    "level": "adequado com apoio",
                },
                "motor": {
                    "title": "Desenvolvimento motor",
                    "percentage": 76,
                    "level": "compatível com a faixa",
                },
            }
        },
    },
    {
        "key": "portage-follow-up",
        "protocol_id": "portage",
        "days_ago": 45,
        "result": "Desenvolvimento global em progresso",
        "percentage": 72,
        "interpretation": (
            "Evolução em socialização, cognição e comunicação funcional. Linguagem expressiva "
            "permanece como área prioritária do plano terapêutico."
        ),
        "fields": [
            {"label": "Socialização", "value": "Participa de brincadeiras compartilhadas"},
            {"label": "Linguagem", "value": "Palavras funcionais e combinações emergentes"},
            {"label": "Cognição", "value": "Resolve problemas simples com modelagem"},
            {"label": "Desenvolvimento motor", "value": "Desempenho compatível com a faixa"},
        ],
        "answers": {
            "ageBand": "ei",
            "completedDomains": ["socializacao", "linguagem", "cognicao", "motor"],
        },
        "scores": {
            "domains": {
                "socializacao": {"title": "Socialização", "percentage": 78},
                "linguagem": {"title": "Linguagem", "percentage": 62},
                "cognicao": {"title": "Cognição", "percentage": 71},
                "motor": {"title": "Desenvolvimento motor", "percentage": 82},
            }
        },
    },
)

DEMO_GOALS: tuple[tuple[str, int, str, str, int, str], ...] = (
    (
        "functional-vocabulary",
        150,
        "Ampliar vocabulário funcional",
        "Linguagem",
        70,
        "Em andamento",
    ),
    (
        "two-word-combinations",
        90,
        "Combinar duas palavras espontaneamente",
        "Linguagem",
        45,
        "Inicial",
    ),
)

DEMO_DOMAIN_SNAPSHOTS: tuple[tuple[str, str, int, int], ...] = (
    ("linguagem", "Linguagem", 150, 42),
    ("linguagem", "Linguagem", 90, 55),
    ("linguagem", "Linguagem", 30, 68),
    ("social", "Socialização", 150, 50),
    ("social", "Socialização", 90, 63),
    ("social", "Socialização", 30, 76),
    ("atencao", "Atenção", 150, 48),
    ("atencao", "Atenção", 90, 58),
    ("atencao", "Atenção", 30, 69),
)


@dataclass(frozen=True, slots=True)
class DemoHistoryChanges:
    anamnese_entries: int = 0
    evolutions: int = 0
    assessments: int = 0
    goals: int = 0
    domain_snapshots: int = 0

    @property
    def total(self) -> int:
        return (
            self.anamnese_entries
            + self.evolutions
            + self.assessments
            + self.goals
            + self.domain_snapshots
        )


def demo_record_uuid(patient_id: UUID, record_type: str, key: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"https://korusfono.com.br/demo/{patient_id}/{record_type}/{key}")


async def ensure_demo_patient_clinical_history(
    db: AsyncSession,
    professional: Professional,
    patient: Patient,
    *,
    today: date | None = None,
) -> DemoHistoryChanges:
    """Add canonical editable demo history without overwriting user-entered data."""
    if not patient.is_demo or patient.professional_id != professional.id:
        raise ValueError("O histórico demonstrativo só pode ser aplicado ao paciente demo do profissional")

    existing_sections = set(
        (
            await db.execute(
                select(AnamneseEntry.section).where(AnamneseEntry.patient_id == patient.id)
            )
        ).scalars()
    )
    added_anamnese_entries = 0
    for section, value in DEMO_ANAMNESIS_ENTRIES:
        if section not in existing_sections:
            db.add(AnamneseEntry(patient_id=patient.id, section=section, value=value))
            added_anamnese_entries += 1

    reference_date = today or date.today()

    evolution_ids = {
        demo_record_uuid(patient.id, "evolution", key)
        for key, _days_ago, _title, _content in DEMO_EVOLUTIONS
    }
    existing_evolution_ids = set(
        (
            await db.execute(select(Evolution.id).where(Evolution.id.in_(evolution_ids)))
        ).scalars()
    )
    added_evolutions = 0
    for key, days_ago, title, content in DEMO_EVOLUTIONS:
        evolution_id = demo_record_uuid(patient.id, "evolution", key)
        if evolution_id in existing_evolution_ids:
            continue
        db.add(
            Evolution(
                id=evolution_id,
                patient_id=patient.id,
                professional_id=professional.id,
                session_id=None,
                date=datetime.combine(
                    reference_date - timedelta(days=days_ago),
                    time(hour=14),
                    tzinfo=UTC,
                ),
                title=title,
                content=content,
            )
        )
        added_evolutions += 1

    required_protocol_ids = {spec["protocol_id"] for spec in DEMO_ASSESSMENTS}
    available_protocol_ids = set(
        (
            await db.execute(
                select(ProtocolCatalog.id).where(ProtocolCatalog.id.in_(required_protocol_ids))
            )
        ).scalars()
    )
    missing_protocol_ids = required_protocol_ids - available_protocol_ids
    if missing_protocol_ids:
        raise RuntimeError(
            "Protocolos necessários ao paciente demo não encontrados: "
            + ", ".join(sorted(missing_protocol_ids))
        )

    assessment_ids = {
        demo_record_uuid(patient.id, "assessment", spec["key"])
        for spec in DEMO_ASSESSMENTS
    }
    existing_assessment_ids = set(
        (
            await db.execute(select(Assessment.id).where(Assessment.id.in_(assessment_ids)))
        ).scalars()
    )
    added_assessments = 0
    for spec in DEMO_ASSESSMENTS:
        assessment_id = demo_record_uuid(patient.id, "assessment", spec["key"])
        if assessment_id in existing_assessment_ids:
            continue
        db.add(
            Assessment(
                id=assessment_id,
                patient_id=patient.id,
                professional_id=professional.id,
                protocol_id=spec["protocol_id"],
                date=reference_date - timedelta(days=spec["days_ago"]),
                result=spec["result"],
                percentage=spec["percentage"],
                interpretation=spec["interpretation"],
                fields=spec["fields"],
                answers=spec["answers"],
                scores=spec["scores"],
                status="completed",
                informant="Responsável — dados fictícios",
                assessment_metadata={
                    "demoSeedVersion": 1,
                    "synthetic": True,
                    "source": "korus_demo_history",
                },
            )
        )
        added_assessments += 1

    goal_ids = {
        demo_record_uuid(patient.id, "goal", key)
        for key, _days_ago, _title, _area, _progress, _status in DEMO_GOALS
    }
    existing_goal_ids = set(
        (await db.execute(select(Goal.id).where(Goal.id.in_(goal_ids)))).scalars()
    )
    added_goals = 0
    for key, days_ago, title, area, progress, status in DEMO_GOALS:
        goal_id = demo_record_uuid(patient.id, "goal", key)
        if goal_id in existing_goal_ids:
            continue
        db.add(
            Goal(
                id=goal_id,
                patient_id=patient.id,
                professional_id=professional.id,
                title=title,
                area=area,
                progress=progress,
                start_date=reference_date - timedelta(days=days_ago),
                status=status,
            )
        )
        added_goals += 1

    snapshot_ids = {
        demo_record_uuid(patient.id, "domain-snapshot", f"{key}-{days_ago}")
        for key, _label, days_ago, _score in DEMO_DOMAIN_SNAPSHOTS
    }
    existing_snapshot_ids = set(
        (
            await db.execute(
                select(ClinicalDomainSnapshot.id).where(
                    ClinicalDomainSnapshot.id.in_(snapshot_ids)
                )
            )
        ).scalars()
    )
    added_domain_snapshots = 0
    for key, label, days_ago, score in DEMO_DOMAIN_SNAPSHOTS:
        snapshot_id = demo_record_uuid(
            patient.id,
            "domain-snapshot",
            f"{key}-{days_ago}",
        )
        if snapshot_id in existing_snapshot_ids:
            continue
        db.add(
            ClinicalDomainSnapshot(
                id=snapshot_id,
                patient_id=patient.id,
                key=key,
                label=label,
                score=score,
                recorded_at=reference_date - timedelta(days=days_ago),
                session_id=None,
            )
        )
        added_domain_snapshots += 1
    await db.flush()
    return DemoHistoryChanges(
        anamnese_entries=added_anamnese_entries,
        evolutions=added_evolutions,
        assessments=added_assessments,
        goals=added_goals,
        domain_snapshots=added_domain_snapshots,
    )


async def ensure_demo_patient(
    db: AsyncSession,
    professional: Professional,
) -> Patient:
    """Return the professional demo patient, recreating it safely when missing."""
    await db.execute(
        select(Professional.id)
        .where(Professional.id == professional.id)
        .with_for_update()
    )
    existing = await db.scalar(
        select(Patient)
        .where(
            Patient.professional_id == professional.id,
            Patient.is_demo.is_(True),
        )
        .order_by(Patient.created_at.asc())
        .limit(1)
    )
    if existing is not None:
        await ensure_demo_patient_clinical_history(db, professional, existing)
        return existing

    patient = Patient(
        professional_id=professional.id,
        name=DEMO_PATIENT_NAME,
        birth_date=demo_patient_birth_date(),
        diagnosis_keys=[],
        status="avaliacao",
        start_date=date.today() - timedelta(days=180),
        avatar_color=DEMO_AVATAR_COLOR,
        is_demo=True,
    )
    db.add(patient)
    await db.flush()
    await ensure_demo_patient_clinical_history(db, professional, patient)
    return patient
