"""Diagnosis catalog keyed by professional specialty."""

from app.core.specialty_catalog import SPECIALTY_KEYS, specialty_label

DiagnosisEntry = dict[str, str]

DIAGNOSIS_CATALOG: dict[str, dict[str, str]] = {
    "fono": {
        "nao_informado": "Não informado na migração",
        "tea": "TEA — Transtorno do Espectro Autista",
        "linguagem": "Atraso de Linguagem",
        "atraso_de_fala": "Atraso de Fala",
        "apraxia": "Apraxia de Fala na Infância",
        "dislexia": "Dislexia",
        "disfagia": "Disfagia",
        "gagueira": "Gagueira",
        "estimulacao_precoce": "Estimulação Precoce",
        "disturbio_fonetico_fonologico": "Distúrbio Fonético-Fonológico",
        "dmo": "DMO — Distúrbio Miofuncional Orofacial",
        "outros": "Outros",
    },
    "to": {
        "nao_informado": "Não informado na migração",
        "tea": "TEA — Transtorno do Espectro Autista",
        "tdah": "TDAH",
        "integracao_sensorial": "Integração Sensorial",
        "atraso_motor": "Atraso Motor",
        "paralisia_cerebral": "Paralisia Cerebral",
        "estimulacao_precoce": "Estimulação Precoce",
        "dificuldades_escolares": "Dificuldades Escolares",
        "outros": "Outros",
    },
    "psicologia": {
        "nao_informado": "Não informado na migração",
        "tea": "TEA — Transtorno do Espectro Autista",
        "tdah": "TDAH",
        "ansiedade": "Ansiedade",
        "depressao": "Depressão",
        "toc": "TOC",
        "dificuldades_aprendizagem": "Dificuldades de Aprendizagem",
        "outros": "Outros",
    },
    "fisioterapia": {
        "nao_informado": "Não informado na migração",
        "atraso_motor": "Atraso Motor",
        "paralisia_cerebral": "Paralisia Cerebral",
        "torticolis": "Tortícolis",
        "plagiocefalia": "Plagiocefalia",
        "deformidades_posturais": "Deformidades Posturais",
        "outros": "Outros",
    },
}


def get_catalog_for_specialty(specialty_key: str) -> dict[str, str]:
    return DIAGNOSIS_CATALOG.get(specialty_key, DIAGNOSIS_CATALOG["fono"])


def list_diagnoses(specialty_key: str) -> list[DiagnosisEntry]:
    catalog = get_catalog_for_specialty(specialty_key)
    return [{"key": key, "label": label} for key, label in catalog.items()]


def diagnosis_label(key: str, specialty_key: str = "fono") -> str:
    catalog = get_catalog_for_specialty(specialty_key)
    return catalog.get(key, key)


def diagnosis_labels(keys: list[str], specialty_key: str) -> list[str]:
    return [diagnosis_label(k, specialty_key) for k in keys]


def validate_diagnosis_keys(keys: list[str], specialty_key: str) -> None:
    if not keys:
        raise ValueError("Pelo menos um diagnóstico é obrigatório")
    catalog = get_catalog_for_specialty(specialty_key)
    invalid = [k for k in keys if k not in catalog]
    if invalid:
        raise ValueError(f"Diagnósticos inválidos para a especialidade: {', '.join(invalid)}")
