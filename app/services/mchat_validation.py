from datetime import date

from fastapi import HTTPException, status

from app.models.patient import Patient
from app.schemas.clinical import AssessmentCreate

MCHAT_PROTOCOL_IDS = frozenset({"mchat", "m-chat-r", "m-chat"})
MCHAT_AT_RISK_YES = frozenset({2, 5, 12})


def patient_age_months(birth_date: date, reference: date | None = None) -> int:
    today = reference or date.today()
    months = (today.year - birth_date.year) * 12 + today.month - birth_date.month
    if today.day < birth_date.day:
        months -= 1
    return max(0, months)


def _invalid(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def _is_valid_informant(value: str | None) -> bool:
    normalized = (value or "").strip()
    return len(normalized) >= 2 and any(character.isalpha() for character in normalized)


def _risk_item_ids(answers: dict) -> list[int]:
    failed: list[int] = []
    for item_id in range(1, 21):
        value = answers.get(str(item_id))
        if value not in {"sim", "nao"}:
            raise _invalid("Responda todos os 20 itens do M-CHAT-R com Sim ou Não")
        at_risk = value == "sim" if item_id in MCHAT_AT_RISK_YES else value == "nao"
        if at_risk:
            failed.append(item_id)
    return failed


def _risk_level(failed: int) -> str:
    if failed >= 8:
        return "elevado"
    if failed >= 3:
        return "medio"
    return "baixo"


def validate_mchat_submission(
    patient: Patient,
    protocol_id: str,
    body: AssessmentCreate,
) -> None:
    if protocol_id.lower() not in MCHAT_PROTOCOL_IDS or body.status != "completed":
        return
    if not _is_valid_informant(body.informant):
        raise _invalid(
            "Informe o nome ou vínculo do informante, como Mãe, Pai ou Terapeuta"
        )

    failed_items = _risk_item_ids(body.answers or {})
    failed_count = len(failed_items)
    level = _risk_level(failed_count)
    scores = body.scores or {}
    metadata = dict(body.metadata or {})

    if scores.get("stage1Failed") != failed_count or scores.get("stage1Level") != level:
        raise _invalid("A pontuação informada não corresponde às respostas do M-CHAT-R")
    if metadata.get("mchatStage1Failed") != failed_count:
        raise _invalid("Os metadados da pontuação do M-CHAT-R estão inconsistentes")

    age_months = patient_age_months(patient.birth_date)
    if 16 <= age_months <= 30:
        age_range = "standard"
    elif 31 <= age_months <= 48:
        age_range = "extended"
    else:
        age_range = "outside"
    if age_range != "standard" and metadata.get(
        "mchatAgeOutsideStandardAcknowledged"
    ) is not True:
        raise _invalid(
            "O M-CHAT-R/F tem faixa principal de 16 a 30 meses; "
            "registre a ciência do profissional para concluir fora dessa faixa"
        )

    if level == "medio":
        raw_outcomes = scores.get("followUpOutcomes")
        if not isinstance(raw_outcomes, dict):
            raise _invalid("Complete a consulta de seguimento dos itens de risco")
        outcomes = {str(key): value for key, value in raw_outcomes.items()}
        expected_keys = {str(item_id) for item_id in failed_items}
        if set(outcomes) != expected_keys or any(
            value not in {"pass", "fail"} for value in outcomes.values()
        ):
            raise _invalid("Complete a consulta de seguimento de todos os itens de risco")
        if any(
            not any(key.startswith(f"fu_{item_id}_") for key in body.answers)
            for item_id in failed_items
        ):
            raise _invalid("As respostas da consulta de seguimento estão incompletas")
        follow_up_failed = sum(value == "fail" for value in outcomes.values())
        if scores.get("followUpFailed") != follow_up_failed:
            raise _invalid("A pontuação da consulta de seguimento está inconsistente")
        if metadata.get("mchatStage") != "full":
            raise _invalid("A etapa da consulta de seguimento está inconsistente")
    elif metadata.get("mchatStage") != "screening_only":
        raise _invalid("A etapa informada do M-CHAT-R está inconsistente")

    metadata["mchatAgeMonths"] = age_months
    metadata["mchatAgeRange"] = age_range
    body.metadata = metadata
