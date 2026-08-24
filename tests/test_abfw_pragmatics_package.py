"""Regressions for the clinician-facing wording of ABFW pragmatics items."""

from app.data.abfw.build_package import PRAGMATICS_ITEMS
from app.services.instrument_content_package import (
    clear_instrument_content_package_cache,
    get_instrument_content_package,
)


EXPECTED_PRAGMATICS_ITEMS = [
    (
        "prag_01",
        "Atos comunicativos",
        "A criança estabelece contato visual adequado durante a interação?",
    ),
    (
        "prag_02",
        "Atos comunicativos",
        "A criança inicia interações ou trocas comunicativas?",
    ),
    (
        "prag_03",
        "Atos comunicativos",
        "A criança mantém a alternância de turnos durante a conversa?",
    ),
    (
        "prag_04",
        "Atos comunicativos",
        "A criança utiliza gestos com finalidade comunicativa?",
    ),
    (
        "prag_05",
        "Funções",
        "A criança utiliza a comunicação para pedir ou solicitar algo?",
    ),
    (
        "prag_06",
        "Funções",
        "A criança utiliza a comunicação para proibir ou orientar alguém?",
    ),
    (
        "prag_07",
        "Funções",
        "A criança utiliza a comunicação para cumprimentar ou se despedir?",
    ),
    (
        "prag_08",
        "Funções",
        "A criança utiliza a comunicação para fazer perguntas ou investigar?",
    ),
    (
        "prag_09",
        "Funções",
        "A criança utiliza a comunicação em brincadeiras simbólicas ou imaginativas?",
    ),
    (
        "prag_10",
        "Meios",
        "A criança utiliza adequadamente recursos verbais?",
    ),
    (
        "prag_11",
        "Meios",
        "A criança utiliza adequadamente recursos vocais?",
    ),
    (
        "prag_12",
        "Meios",
        "A criança utiliza adequadamente recursos gestuais?",
    ),
    (
        "prag_13",
        "Narrativa",
        "A criança organiza os acontecimentos em sequência ao narrar?",
    ),
    (
        "prag_14",
        "Narrativa",
        "A criança mantém coerência com o tema durante a narrativa?",
    ),
    (
        "prag_15",
        "Narrativa",
        "A criança faz referência a personagens e às ações deles durante a narrativa?",
    ),
]


def test_abfw_pragmatics_uses_reworded_questions_without_changing_item_identity():
    clear_instrument_content_package_cache()
    package = get_instrument_content_package("abfw")

    items = package.get_module_items("pragmatica")

    assert items == PRAGMATICS_ITEMS
    assert [(item["id"], item["category"], item["text"]) for item in items] == (
        EXPECTED_PRAGMATICS_ITEMS
    )
