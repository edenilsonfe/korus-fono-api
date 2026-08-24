#!/usr/bin/env python3
"""Gera manifest.json e arquivos de itens do pacote ABFW (estrutura fiel, conteúdo público)."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# 39 vocábulos — prova de imitação (ABFW)
IMITATION_WORDS = [
    "peteca", "bandeja", "tigela", "doce", "cortina", "gato", "foguete", "vinho",
    "selo", "zero", "chuva", "jacaré", "machado", "nata", "lama", "lápis", "prego",
    "café", "alface", "raposa", "borracha", "abelha", "carro", "branco", "travessa",
    "droga", "cravo", "grosso", "fraco", "plástico", "bloco", "clube", "globo",
    "flauta", "pastel", "porco", "nariz", "amor", "roupa",
]

# 34 figuras — prova de nomeação (ABFW)
NAMING_FIGURES = [
    "palhaço", "bolsa", "tesoura", "cadeira", "galinha", "vassoura", "cebola",
    "xícara", "mesa", "navio", "livro", "sapo", "tambor", "sapato", "balde",
    "faca", "fogão", "peixe", "relógio", "cama", "anel", "milho", "cachorro",
    "blusa", "garfo", "trator", "prato", "pasta", "dedo", "braço", "girafa",
    "zebra", "planta", "cruz",
]

# Vocabulário — 118 figuras em 9 campos conceituais
VOCABULARY_FIELDS: list[tuple[str, str, list[str]]] = [
    ("animais", "Animais", [
        "cachorro", "gato", "pássaro", "peixe", "cavalo", "vaca", "porco", "coelho",
        "elefante", "leão", "macaco", "sapo", "borboleta", "abelha", "formiga",
    ]),
    ("alimentos", "Alimentos", [
        "maçã", "banana", "laranja", "uva", "melancia", "pão", "queijo", "leite",
        "ovo", "arroz", "feijão", "carne", "peixe", "sopa", "bolo",
    ]),
    ("vestuario", "Vestuário", [
        "camisa", "calça", "vestido", "saia", "meia", "sapato", "chapéu", "luva",
        "casaco", "short", "tênis", "sandália",
    ]),
    ("transportes", "Transportes", [
        "carro", "ônibus", "caminhão", "trem", "avião", "barco", "bicicleta",
        "motocicleta", "helicóptero", "metrô", "patinete", "trator",
    ]),
    ("moveis", "Móveis e utensílios", [
        "cama", "mesa", "cadeira", "sofá", "armário", "geladeira", "fogão",
        "panela", "copo", "prato", "colher", "garfo", "faca", "escova",
    ]),
    ("profissoes", "Profissões", [
        "médico", "professor", "bombeiro", "policial", "dentista", "enfermeiro",
        "carteiro", "padeiro", "cabeleireiro", "motorista", "cozinheiro", "agricultor",
    ]),
    ("locais", "Locais", [
        "casa", "escola", "hospital", "igreja", "praia", "parque", "loja",
        "mercado", "fazenda", "praça", "cinema", "biblioteca",
    ]),
    ("formas", "Formas e cores", [
        "círculo", "quadrado", "triângulo", "retângulo", "vermelho", "azul",
        "verde", "amarelo", "preto", "branco", "rosa", "laranja",
    ]),
    ("brinquedos", "Brinquedos e instrumentos", [
        "bola", "boneca", "carrinho", "pipa", "bloco", "urso", "violino", "piano",
        "flauta", "tambor", "xilofone", "violão", "trompete", "guitarra",
    ]),
]

PHONOLOGICAL_PROCESSES = [
    {"id": "fronting", "label": "Frontalização", "expected_age_months": 48},
    {"id": "backing", "label": "Reversão velar", "expected_age_months": 48},
    {"id": "stopping", "label": "Oclusivação", "expected_age_months": 60},
    {"id": "cluster_reduction", "label": "Simplificação de encontro consonantal", "expected_age_months": 60},
    {"id": "final_consonant_deletion", "label": "Supressão de coda", "expected_age_months": 36},
    {"id": "weak_syllable_deletion", "label": "Supressão de sílaba átona", "expected_age_months": 48},
    {"id": "gliding", "label": "Liquidação", "expected_age_months": 60},
    {"id": "deaffrication", "label": "Desafricamento", "expected_age_months": 48},
    {"id": "vocalization", "label": "Vocalização", "expected_age_months": 36},
    {"id": "assimilation", "label": "Assimilação", "expected_age_months": 36},
    {"id": "metathesis", "label": "Metátese", "expected_age_months": 48},
    {"id": "epenthesis", "label": "Epêntese", "expected_age_months": 48},
    {"id": "coalescence", "label": "Coalescência", "expected_age_months": 60},
    {"id": "depalatalization", "label": "Despalatalização", "expected_age_months": 48},
    {"id": "other", "label": "Outro", "expected_age_months": 999},
]

FLUENCY_DISFLUENCY_TYPES = [
    {"id": "segment_repetition", "label": "Repetição de segmento", "category": "common"},
    {"id": "syllable_repetition", "label": "Repetição de sílaba", "category": "common"},
    {"id": "word_repetition", "label": "Repetição de palavra", "category": "common"},
    {"id": "phrase_repetition", "label": "Repetição de frase", "category": "common"},
    {"id": "prolongation", "label": "Prolongamento", "category": "stuttering"},
    {"id": "block", "label": "Bloqueio", "category": "stuttering"},
    {"id": "pause", "label": "Pausa", "category": "common"},
    {"id": "interjection", "label": "Interjeição", "category": "common"},
    {"id": "revision", "label": "Revisão", "category": "common"},
    {"id": "incomplete_phrase", "label": "Frase incompleta", "category": "common"},
]

PRAGMATICS_ITEMS = [
    {
        "id": "prag_01",
        "category": "Atos comunicativos",
        "text": "A criança estabelece contato visual adequado durante a interação?",
    },
    {
        "id": "prag_02",
        "category": "Atos comunicativos",
        "text": "A criança inicia interações ou trocas comunicativas?",
    },
    {
        "id": "prag_03",
        "category": "Atos comunicativos",
        "text": "A criança mantém a alternância de turnos durante a conversa?",
    },
    {
        "id": "prag_04",
        "category": "Atos comunicativos",
        "text": "A criança utiliza gestos com finalidade comunicativa?",
    },
    {
        "id": "prag_05",
        "category": "Funções",
        "text": "A criança utiliza a comunicação para pedir ou solicitar algo?",
    },
    {
        "id": "prag_06",
        "category": "Funções",
        "text": "A criança utiliza a comunicação para proibir ou orientar alguém?",
    },
    {
        "id": "prag_07",
        "category": "Funções",
        "text": "A criança utiliza a comunicação para cumprimentar ou se despedir?",
    },
    {
        "id": "prag_08",
        "category": "Funções",
        "text": "A criança utiliza a comunicação para fazer perguntas ou investigar?",
    },
    {
        "id": "prag_09",
        "category": "Funções",
        "text": "A criança utiliza a comunicação em brincadeiras simbólicas ou imaginativas?",
    },
    {
        "id": "prag_10",
        "category": "Meios",
        "text": "A criança utiliza adequadamente recursos verbais?",
    },
    {
        "id": "prag_11",
        "category": "Meios",
        "text": "A criança utiliza adequadamente recursos vocais?",
    },
    {
        "id": "prag_12",
        "category": "Meios",
        "text": "A criança utiliza adequadamente recursos gestuais?",
    },
    {
        "id": "prag_13",
        "category": "Narrativa",
        "text": "A criança organiza os acontecimentos em sequência ao narrar?",
    },
    {
        "id": "prag_14",
        "category": "Narrativa",
        "text": "A criança mantém coerência com o tema durante a narrativa?",
    },
    {
        "id": "prag_15",
        "category": "Narrativa",
        "text": "A criança faz referência a personagens e às ações deles durante a narrativa?",
    },
]


def _phonology_items(words: list[str], prefix: str, stimulus_type: str) -> list[dict]:
    return [
        {
            "id": f"{prefix}_{idx:02d}",
            "target": word,
            "text": word,
            "stimulus_type": stimulus_type,
            "category": "fonologia",
        }
        for idx, word in enumerate(words, start=1)
    ]


def _vocabulary_items() -> list[dict]:
    items: list[dict] = []
    for field_id, field_title, words in VOCABULARY_FIELDS:
        for idx, word in enumerate(words, start=1):
            items.append(
                {
                    "id": f"voc_{field_id}_{idx:02d}",
                    "target": word,
                    "text": word,
                    "stimulus_type": "figure",
                    "category": field_id,
                    "category_title": field_title,
                }
            )
    return items


def build() -> None:
    fon_imitacao = _phonology_items(IMITATION_WORDS, "fon_im", "word")
    fon_nomeacao = _phonology_items(NAMING_FIGURES, "fon_nm", "figure")
    vocabulario = _vocabulary_items()

    fluencia_items = [
        {"id": "flu_session", "text": "Amostra de fala — leitura até 200 sílabas", "stimulus_type": "session"},
        *[
            {"id": f"flu_{d['id']}", "text": d["label"], "stimulus_type": "counter", "category": d["category"]}
            for d in FLUENCY_DISFLUENCY_TYPES
        ],
    ]

    (ROOT / "items").mkdir(exist_ok=True)
    for name, data in [
        ("fonologia-imitacao.json", fon_imitacao),
        ("fonologia-nomeacao.json", fon_nomeacao),
        ("vocabulario.json", vocabulario),
        ("fluencia.json", fluencia_items),
        ("pragmatica.json", PRAGMATICS_ITEMS),
    ]:
        (ROOT / "items" / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    categories_vocab = [{"id": fid, "title": title} for fid, title, _ in VOCABULARY_FIELDS]

    manifest = {
        "version": 2,
        "package_id": "abfw-br-v2",
        "instrument_slug": "abfw",
        "instrument_title": "ABFW — Teste de Linguagem Infantil",
        "publisher": "Estrutura fiel — conteúdo público",
        "license_ref": "PUBLIC-STRUCTURE",
        "norms_region": "BR",
        "archetype": "battery",
        "supports_multi_session": True,
        "phonological_processes": PHONOLOGICAL_PROCESSES,
        "classifications": {
            "phonology": [
                {"id": "correct", "label": "Correta"},
                {"id": "substitution", "label": "Substituição"},
                {"id": "omission", "label": "Omissão"},
                {"id": "distortion", "label": "Distorsão"},
                {"id": "no_response", "label": "Não respondeu"},
            ],
            "vocabulary": [
                {"id": "dvu", "label": "DVU — Denominação Vocabulário Usual"},
                {"id": "nd", "label": "ND — Não Denominado"},
                {"id": "ps", "label": "PS — Processo de Substituição"},
                {"id": "omission", "label": "Omissão"},
                {"id": "no_response", "label": "Não respondeu"},
            ],
            "pragmatics": [
                {"id": "adequate", "label": "Adequado"},
                {"id": "attention", "label": "Atenção"},
                {"id": "altered", "label": "Alterado"},
            ],
        },
        "domains": [
            {"id": "fonologia", "title": "Fonologia"},
            {"id": "vocabulario", "title": "Vocabulário"},
            {"id": "fluencia", "title": "Fluência"},
            {"id": "pragmatica", "title": "Pragmática"},
        ],
        "modules": {
            "fonologia-imitacao": {
                "id": "fonologia-imitacao",
                "title": "Fonologia — Imitação",
                "domain": "fonologia",
                "module_kind": "phonology",
                "module_subtype": "imitation",
                "items_file": "items/fonologia-imitacao.json",
                "item_count": len(fon_imitacao),
                "filler": "clinician",
            },
            "fonologia-nomeacao": {
                "id": "fonologia-nomeacao",
                "title": "Fonologia — Nomeação",
                "domain": "fonologia",
                "module_kind": "phonology",
                "module_subtype": "naming",
                "items_file": "items/fonologia-nomeacao.json",
                "item_count": len(fon_nomeacao),
                "filler": "clinician",
            },
            "vocabulario": {
                "id": "vocabulario",
                "title": "Vocabulário Expressivo",
                "domain": "vocabulario",
                "module_kind": "vocabulary",
                "categories": categories_vocab,
                "items_file": "items/vocabulario.json",
                "item_count": len(vocabulario),
                "filler": "clinician",
            },
            "fluencia": {
                "id": "fluencia",
                "title": "Fluência",
                "domain": "fluencia",
                "module_kind": "fluency",
                "target_syllables": 200,
                "items_file": "items/fluencia.json",
                "item_count": len(fluencia_items),
                "filler": "clinician",
            },
            "pragmatica": {
                "id": "pragmatica",
                "title": "Pragmática",
                "domain": "pragmatica",
                "module_kind": "pragmatics",
                "items_file": "items/pragmatica.json",
                "item_count": len(PRAGMATICS_ITEMS),
                "filler": "clinician",
            },
        },
        "scoring": {"engine": "battery_module_kind"},
        "report": {
            "template_id": "abfw-br-v1",
            "sections": ["identificacao", "resultados", "graficos", "observacoes", "resumo", "assinatura"],
        },
        "norms_file": "norms-br.json",
    }

    # fix subtests
    manifest["subtests"] = [
        {"id": slug, "title": mod["title"], "item_count": mod["item_count"]}
        for slug, mod in manifest["modules"].items()
    ]

    (ROOT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    norms = {
        "status": "public_reference",
        "region": "BR",
        "note": "Referências etárias aproximadas para heatmap; substituir por normas licenciadas.",
        "vocabulary_reference_by_age_months": {
            "24": {"animais": 40, "alimentos": 35, "vestuario": 30, "transportes": 25},
            "36": {"animais": 60, "alimentos": 55, "vestuario": 50, "transportes": 45, "moveis": 40},
            "48": {"animais": 75, "alimentos": 70, "vestuario": 65, "transportes": 60, "moveis": 55, "profissoes": 40},
            "60": {"animais": 85, "alimentos": 80, "vestuario": 75, "transportes": 70, "moveis": 65, "profissoes": 55, "locais": 50},
            "72": {"animais": 90, "alimentos": 85, "vestuario": 80, "transportes": 75, "moveis": 70, "profissoes": 65, "locais": 60, "formas": 55, "brinquedos": 50},
        },
        "fluency_reference": {
            "syllables_per_minute_min": 120,
            "syllables_per_minute_max": 200,
            "disfluency_percent_max": 3,
        },
    }
    (ROOT / "norms-br.json").write_text(json.dumps(norms, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Pacote ABFW gerado em {ROOT}")


if __name__ == "__main__":
    build()
