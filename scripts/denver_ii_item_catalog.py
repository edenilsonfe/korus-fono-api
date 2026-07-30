"""Denver II item catalog — UNIFESP/EPM Portuguese translation (Pedromônico et al., 1999).

Source: "Teste Denver II" (tradução e adaptação transcultural brasileira), Pedromônico MRM
et al., Escola Paulista de Medicina — UNIFESP, 1999. Full item list (125 itens) organizado
nos quatro domínios originais do Denver II (Frankenburg & Dodds, 1990/1992): Pessoal-Social
(PS), Motor Fino-Adaptativo (FM), Linguagem (LA) e Motor Grosso (MG).

Idades (``age_start_months`` / ``age_end_months``) aproximam os percentis 25%/90% típicos
das barras etárias do Denver II, em meses inteiros — não são as tabelas licenciadas do
editor original e devem ser tratadas como referência aproximada para triagem, até a
publicação de um kit licenciado com as barras oficiais brasileiras (ver ``norms-br.json``).

``report_item`` marca os itens identificados com "(An)" na fonte — habilidades cuja
informação é obtida por relato do cuidador ("Anamnese"/informante), em vez de observação
direta pelo examinador durante a aplicação.
"""

from __future__ import annotations

# Each row: (item_number, text, age_start_months, age_end_months, report_item, examiner_instructions)
ItemRow = tuple[int, str, int, int, bool, str]


def _build(domain: str, prefix: str, rows: list[ItemRow]) -> list[dict]:
    return [
        {
            "id": f"{prefix}_{num:02d}",
            "domain": domain,
            "text": text,
            "age_start_months": age_start,
            "age_end_months": age_end,
            "report_item": report_item,
            "examiner_instructions": instructions,
        }
        for num, text, age_start, age_end, report_item, instructions in rows
    ]


# ---------------------------------------------------------------------------
# Pessoal-Social (PS) — 25 itens
# ---------------------------------------------------------------------------
_PS_ROWS: list[ItemRow] = [
    (
        1,
        "Observa um rosto",
        0,
        2,
        False,
        "Procedimento: aproxime o rosto do bebê dentro do campo visual, sem falar. "
        "Critério (Passa): o bebê fixa o olhar no rosto do examinador por alguns segundos.",
    ),
    (
        2,
        "Sorri em resposta",
        1,
        3,
        False,
        "Procedimento: sorria e fale suavemente com o bebê, sem tocá-lo. "
        "Critério (Passa): o bebê sorri em resposta ao rosto/voz do examinador.",
    ),
    (
        3,
        "Sorri espontaneamente (An)",
        2,
        4,
        True,
        "Pergunte ao cuidador se o bebê sorri espontaneamente, sem estímulo direto "
        "(ex.: ao acordar, ao ver algo familiar). Critério (Passa): cuidador relata "
        "sorrisos espontâneos frequentes.",
    ),
    (
        4,
        "Observa sua própria mão (An)",
        2,
        5,
        True,
        "Pergunte ao cuidador se o bebê observa e brinca com as próprias mãos por "
        "alguns momentos. Critério (Passa): cuidador confirma esse comportamento.",
    ),
    (
        5,
        "Tenta alcançar um brinquedo",
        3,
        6,
        False,
        "Procedimento: ofereça um brinquedo pequeno ao alcance do bebê, sem colocá-lo "
        "na mão dele. Critério (Passa): o bebê estende o braço/mão em direção ao brinquedo.",
    ),
    (
        6,
        "Come sozinho (An)",
        5,
        8,
        True,
        "Pergunte ao cuidador se a criança leva alimentos (biscoito, pedaços macios) "
        "à boca sozinha. Critério (Passa): cuidador confirma que a criança se alimenta "
        "sozinha com as mãos.",
    ),
    (
        7,
        "Bate palmas (An)",
        7,
        11,
        True,
        "Pergunte ao cuidador se a criança bate palmas, sozinha ou imitando o adulto. "
        "Critério (Passa): cuidador relata que a criança bate palmas.",
    ),
    (
        8,
        "Mostra o que quer (não com choro) (An)",
        8,
        13,
        True,
        "Pergunte ao cuidador se a criança indica o que deseja apontando, gesticulando "
        "ou vocalizando, sem recorrer apenas ao choro. Critério (Passa): cuidador "
        "confirma comunicação não verbal de desejos.",
    ),
    (
        9,
        "Dá tchau (An)",
        8,
        13,
        True,
        "Pergunte ao cuidador se a criança acena tchau espontaneamente ou quando "
        "solicitada. Critério (Passa): cuidador confirma o gesto de despedida.",
    ),
    (
        10,
        "Joga bola com o examinador",
        9,
        14,
        False,
        "Procedimento: role uma bola em direção à criança e peça que ela a devolva. "
        "Critério (Passa): a criança devolve a bola rolando-a de volta ao examinador.",
    ),
    (
        11,
        "Imita a ação de uma pessoa (An)",
        9,
        14,
        True,
        "Pergunte ao cuidador se a criança imita ações simples de adultos (ex.: "
        "varrer, falar ao telefone). Critério (Passa): cuidador relata imitação de "
        "pelo menos uma ação.",
    ),
    (
        12,
        "Bebe em uma xícara ou copo (An)",
        10,
        16,
        True,
        "Pergunte ao cuidador se a criança bebe sozinha em copo ou xícara, com pouco "
        "derramamento. Critério (Passa): cuidador confirma essa habilidade.",
    ),
    (
        13,
        "Ajuda em casa (tarefas simples) (An)",
        12,
        21,
        True,
        "Pergunte ao cuidador se a criança ajuda em pequenas tarefas domésticas "
        "quando solicitada (ex.: guardar objeto, buscar item). Critério (Passa): "
        "cuidador confirma colaboração em ao menos uma tarefa simples.",
    ),
    (
        14,
        "Usa colher/garfo (An)",
        13,
        20,
        True,
        "Pergunte ao cuidador se a criança usa colher ou garfo para se alimentar, "
        "com derramamento mínimo. Critério (Passa): cuidador confirma o uso funcional "
        "do talher.",
    ),
    (
        15,
        "Retira uma vestimenta (An)",
        13,
        20,
        True,
        "Pergunte ao cuidador se a criança consegue tirar uma peça de roupa simples "
        "(meia, sapato) sozinha. Critério (Passa): cuidador confirma a habilidade.",
    ),
    (
        16,
        "Alimenta uma boneca",
        15,
        23,
        False,
        "Procedimento: ofereça uma boneca e uma mamadeira/colher de brinquedo e peça "
        "que a criança a alimente. Critério (Passa): a criança simula alimentar a "
        "boneca de forma dirigida.",
    ),
    (
        17,
        "Veste-se (com supervisão) (An)",
        15,
        24,
        True,
        "Pergunte ao cuidador se a criança participa ativamente de vestir-se, mesmo "
        "precisando de ajuda/supervisão. Critério (Passa): cuidador confirma "
        "participação ativa no processo.",
    ),
    (
        18,
        "Escova os dentes com ajuda (An)",
        17,
        27,
        True,
        "Pergunte ao cuidador se a criança escova os dentes com auxílio de um adulto. "
        "Critério (Passa): cuidador confirma a colaboração na escovação assistida.",
    ),
    (
        19,
        "Lava e seca as mãos (An)",
        20,
        30,
        True,
        "Pergunte ao cuidador se a criança lava e seca as mãos sozinha, sem "
        "necessidade de ajuda direta. Critério (Passa): cuidador confirma a "
        "autonomia nessa tarefa.",
    ),
    (
        20,
        "Fala o nome de amigos",
        21,
        31,
        False,
        "Procedimento: pergunte à criança o nome de um amigo ou colega. Critério "
        "(Passa): a criança fornece pelo menos um nome próprio de um amigo/colega.",
    ),
    (
        21,
        "Veste uma camiseta (An)",
        24,
        33,
        True,
        "Pergunte ao cuidador se a criança consegue vestir uma camiseta sozinha "
        "(mesmo com pequenos ajustes do adulto). Critério (Passa): cuidador confirma "
        "a habilidade.",
    ),
    (
        22,
        "Veste-se sem ajuda (An)",
        30,
        42,
        True,
        "Pergunte ao cuidador se a criança se veste completamente sozinha, sem "
        "necessidade de ajuda. Critério (Passa): cuidador confirma autonomia total "
        "ao vestir-se.",
    ),
    (
        23,
        "Joga jogos de mesa (An)",
        33,
        48,
        True,
        "Pergunte ao cuidador se a criança participa de jogos de mesa simples "
        "respeitando regras básicas (ex.: turnos). Critério (Passa): cuidador "
        "confirma participação com compreensão mínima das regras.",
    ),
    (
        24,
        "Escova os dentes sem ajuda (An)",
        36,
        52,
        True,
        "Pergunte ao cuidador se a criança escova os dentes sozinha, sem necessidade "
        "de ajuda física do adulto. Critério (Passa): cuidador confirma a autonomia "
        "na escovação.",
    ),
    (
        25,
        "Prepara uma refeição (An)",
        43,
        60,
        True,
        "Pergunte ao cuidador se a criança prepara uma refeição simples sozinha "
        "(ex.: cereal com leite, sanduíche). Critério (Passa): cuidador confirma "
        "que a criança realiza essa tarefa com autonomia.",
    ),
]

PS_ITEMS: list[dict] = _build("PS", "ps", _PS_ROWS)


# ---------------------------------------------------------------------------
# Motor Fino-Adaptativo (FM) — 29 itens
# ---------------------------------------------------------------------------
_FM_ROWS: list[ItemRow] = [
    (
        1,
        "Segue até a linha média",
        0,
        2,
        False,
        "Procedimento: com o bebê em decúbito dorsal, mova um objeto colorido "
        "lentamente até a linha média do campo visual. Critério (Passa): o bebê "
        "acompanha o objeto com o olhar até a linha média.",
    ),
    (
        2,
        "Ultrapassa a linha média",
        1,
        3,
        False,
        "Procedimento: mova o objeto além da linha média, para o lado. Critério "
        "(Passa): o bebê acompanha o movimento do objeto ultrapassando a linha média.",
    ),
    (
        3,
        "Segura um chocalho",
        2,
        4,
        False,
        "Procedimento: coloque um chocalho na mão do bebê. Critério (Passa): o bebê "
        "segura o chocalho por alguns segundos.",
    ),
    (
        4,
        "Junta as mãos",
        2,
        4,
        False,
        "Procedimento: observe o bebê em posição de alerta, sem estimular "
        "diretamente. Critério (Passa): o bebê junta as duas mãos na linha média "
        "do corpo.",
    ),
    (
        5,
        "Segue até 180°",
        3,
        5,
        False,
        "Procedimento: mova o objeto em um arco de 180° diante do bebê. Critério "
        "(Passa): o bebê acompanha o objeto com o olhar por todo o percurso.",
    ),
    (
        6,
        "Olha para um objeto pequeno",
        3,
        5,
        False,
        "Procedimento: apresente um objeto pequeno (ex.: passa/uva-passa) a cerca "
        "de 30 cm do rosto do bebê. Critério (Passa): o bebê fixa o olhar no "
        "objeto pequeno.",
    ),
    (
        7,
        "Tenta alcançar um objeto pequeno",
        3,
        6,
        False,
        "Procedimento: aproxime o objeto pequeno ao alcance da criança. Critério "
        "(Passa): a criança estende a mão em direção ao objeto.",
    ),
    (
        8,
        "Procura o pom-pom",
        5,
        8,
        False,
        "Procedimento: deixe cair um pom-pom (ou objeto pequeno) fora da vista da "
        "criança. Critério (Passa): a criança procura o objeto com o olhar/mão.",
    ),
    (
        9,
        "Pega objeto pequeno",
        5,
        8,
        False,
        "Procedimento: coloque o objeto pequeno ao alcance da criança sobre uma "
        "superfície plana. Critério (Passa): a criança pega o objeto usando toda "
        "a mão (preensão palmar).",
    ),
    (
        10,
        "Transfere um cubo",
        5,
        8,
        False,
        "Procedimento: entregue um cubo a uma das mãos da criança. Critério "
        "(Passa): a criança passa o cubo de uma mão para a outra.",
    ),
    (
        11,
        "Pega dois cubos",
        6,
        9,
        False,
        "Procedimento: ofereça um segundo cubo enquanto a criança já segura o "
        "primeiro. Critério (Passa): a criança segura um cubo em cada mão "
        "simultaneamente.",
    ),
    (
        12,
        "Pinça polegar-dedo",
        7,
        10,
        False,
        "Procedimento: ofereça o objeto pequeno sobre a mesa. Critério (Passa): a "
        "criança pega o objeto usando pinça entre polegar e indicador.",
    ),
    (
        13,
        "Bate dois cubos seguros nas mãos (An)",
        7,
        11,
        True,
        "Pergunte ao cuidador se a criança bate dois cubos (um em cada mão) um "
        "contra o outro. Critério (Passa): cuidador confirma esse comportamento.",
    ),
    (
        14,
        "Coloca bloco na caneca",
        9,
        14,
        False,
        "Procedimento: ofereça um cubo e uma caneca/xícara pequena. Critério "
        "(Passa): a criança coloca o cubo dentro da caneca sem demonstração prévia.",
    ),
    (
        15,
        "Rabisca espontaneamente",
        11,
        17,
        False,
        "Procedimento: ofereça papel e lápis/giz de cera à criança. Critério "
        "(Passa): a criança rabisca espontaneamente, sem demonstração.",
    ),
    (
        16,
        "Retira objeto pequeno, por demonstração",
        12,
        18,
        False,
        "Procedimento: demonstre retirar o objeto pequeno de dentro de um frasco "
        "transparente e peça que a criança repita. Critério (Passa): a criança "
        "retira o objeto após a demonstração.",
    ),
    (
        17,
        "Torre de 2 cubos",
        12,
        19,
        False,
        "Procedimento: demonstre empilhar 2 cubos e peça que a criança repita. "
        "Critério (Passa): a criança empilha 2 cubos sem derrubar.",
    ),
    (
        18,
        "Torre de 4 cubos",
        15,
        22,
        False,
        "Procedimento: demonstre empilhar 4 cubos e peça que a criança repita. "
        "Critério (Passa): a criança empilha 4 cubos.",
    ),
    (
        19,
        "Torre de 6 cubos",
        18,
        27,
        False,
        "Procedimento: demonstre empilhar 6 cubos e peça que a criança repita. "
        "Critério (Passa): a criança empilha 6 cubos.",
    ),
    (
        20,
        "Imita linha vertical",
        18,
        27,
        False,
        "Procedimento: desenhe uma linha vertical diante da criança e peça que ela "
        "copie. Critério (Passa): a criança desenha uma linha com orientação "
        "predominantemente vertical.",
    ),
    (
        21,
        "Torre de 8 cubos",
        21,
        31,
        False,
        "Procedimento: demonstre empilhar 8 cubos e peça que a criança repita. "
        "Critério (Passa): a criança empilha 8 cubos.",
    ),
    (
        22,
        "Move o polegar com a mão fechada",
        22,
        32,
        False,
        "Procedimento: peça que a criança feche a mão e mova apenas o polegar, por "
        "demonstração. Critério (Passa): a criança movimenta o polegar isoladamente, "
        "sem mover os demais dedos.",
    ),
    (
        23,
        "Copia um círculo",
        27,
        38,
        False,
        "Procedimento: desenhe um círculo e peça que a criança copie, sem "
        "demonstração do traçado. Critério (Passa): a criança desenha uma forma "
        "fechada e circular reconhecível.",
    ),
    (
        24,
        "Desenha pessoa (3 partes)",
        31,
        44,
        False,
        "Procedimento: peça que a criança desenhe uma pessoa. Critério (Passa): o "
        "desenho contém ao menos 3 partes do corpo.",
    ),
    (
        25,
        "Copia cruz (+)",
        33,
        47,
        False,
        "Procedimento: desenhe uma cruz (+) e peça que a criança copie. Critério "
        "(Passa): a criança reproduz a cruz com dois traços que se cruzam.",
    ),
    (
        26,
        "Aponta a linha mais comprida",
        36,
        50,
        False,
        "Procedimento: apresente pares de linhas de tamanhos diferentes e peça que "
        "a criança aponte a mais comprida. Critério (Passa): a criança aponta "
        "corretamente a linha mais longa na maioria das tentativas.",
    ),
    (
        27,
        "Copia quadrado, com demonstração",
        40,
        54,
        False,
        "Procedimento: desenhe um quadrado enquanto a criança observa e peça que "
        "ela copie. Critério (Passa): a criança reproduz uma forma com quatro "
        "lados reconhecível.",
    ),
    (
        28,
        "Desenha pessoa (6 partes)",
        43,
        58,
        False,
        "Procedimento: peça que a criança desenhe uma pessoa. Critério (Passa): o "
        "desenho contém ao menos 6 partes do corpo.",
    ),
    (
        29,
        "Copia quadrado",
        48,
        65,
        False,
        "Procedimento: peça que a criança copie um quadrado já desenhado, sem "
        "demonstração do traçado. Critério (Passa): a criança reproduz um "
        "quadrado reconhecível.",
    ),
]

FM_ITEMS: list[dict] = _build("FM", "fm", _FM_ROWS)


# ---------------------------------------------------------------------------
# Linguagem (LA) — 39 itens
# ---------------------------------------------------------------------------
_LA_ROWS: list[ItemRow] = [
    (
        1,
        "Reage ao sino",
        0,
        2,
        False,
        "Procedimento: toque um sino fora do campo visual do bebê. Critério "
        "(Passa): o bebê reage ao som (sobressalto, mudança de atividade, piscar).",
    ),
    (
        2,
        "Vocaliza (An)",
        0,
        2,
        True,
        "Pergunte ao cuidador se o bebê emite sons vocais além do choro. Critério "
        "(Passa): cuidador confirma vocalizações espontâneas.",
    ),
    (
        3,
        "Fala Ooo/Aah (An)",
        1,
        3,
        True,
        "Pergunte ao cuidador se o bebê produz sons vocálicos como 'ooh' ou 'aah'. "
        "Critério (Passa): cuidador confirma esses sons.",
    ),
    (
        4,
        "Riso / gargalhada (An)",
        1,
        3,
        True,
        "Pergunte ao cuidador se o bebê ri alto (gargalha), não apenas sorri. "
        "Critério (Passa): cuidador confirma episódios de riso.",
    ),
    (
        5,
        "Grita (An)",
        2,
        4,
        True,
        "Pergunte ao cuidador se o bebê grita/guincha de forma vocal (não de "
        "choro). Critério (Passa): cuidador confirma esse comportamento vocal.",
    ),
    (
        6,
        "Volta-se para o som",
        3,
        5,
        False,
        "Procedimento: produza um som suave (chocalho) fora do campo visual, "
        "lateralmente. Critério (Passa): o bebê vira a cabeça em direção ao som.",
    ),
    (
        7,
        "Volta-se para a voz",
        3,
        5,
        False,
        "Procedimento: fale suavemente com o bebê fora do seu campo visual. "
        "Critério (Passa): o bebê vira a cabeça em direção à voz.",
    ),
    (
        8,
        "Sílabas isoladas (An)",
        5,
        8,
        True,
        "Pergunte ao cuidador se o bebê produz sílabas isoladas como 'ba', 'da', "
        "'ga'. Critério (Passa): cuidador confirma a produção dessas sílabas.",
    ),
    (
        9,
        "Imita sons (An)",
        6,
        9,
        True,
        "Pergunte ao cuidador se a criança tenta imitar sons da fala do adulto. "
        "Critério (Passa): cuidador confirma tentativas de imitação vocal.",
    ),
    (
        10,
        "Duplica sílabas (An)",
        7,
        10,
        True,
        "Pergunte ao cuidador se a criança duplica sílabas, tipo 'mama', 'papa', "
        "sem significado específico. Critério (Passa): cuidador confirma essa "
        "duplicação.",
    ),
    (
        11,
        "Combina sílabas (An)",
        8,
        11,
        True,
        "Pergunte ao cuidador se a criança combina diferentes sílabas em sequência "
        "(ex.: 'badaga'). Critério (Passa): cuidador confirma a combinação de "
        "sílabas variadas.",
    ),
    (
        12,
        "Jargão (An)",
        9,
        13,
        True,
        "Pergunte ao cuidador se a criança usa 'jargão' — sequências de sons com "
        "entonação de fala, sem palavras reais. Critério (Passa): cuidador "
        "confirma esse padrão vocal.",
    ),
    (
        13,
        "Papa ou mama específico (An)",
        10,
        14,
        True,
        "Pergunte ao cuidador se a criança usa 'papa' ou 'mama' especificamente "
        "para se referir ao pai/mãe. Critério (Passa): cuidador confirma o uso "
        "específico e consistente.",
    ),
    (
        14,
        "1 palavra (An)",
        11,
        15,
        True,
        "Pergunte ao cuidador se a criança fala pelo menos 1 palavra com "
        "significado além de mamãe/papai. Critério (Passa): cuidador confirma a "
        "primeira palavra.",
    ),
    (
        15,
        "2 palavras (An)",
        12,
        18,
        True,
        "Pergunte ao cuidador se a criança fala pelo menos 2 palavras diferentes "
        "com significado. Critério (Passa): cuidador confirma esse vocabulário.",
    ),
    (
        16,
        "3 palavras (An)",
        13,
        20,
        True,
        "Pergunte ao cuidador se a criança usa pelo menos 3 palavras diferentes. "
        "Critério (Passa): cuidador confirma o vocabulário.",
    ),
    (
        17,
        "6 palavras (An)",
        14,
        22,
        True,
        "Pergunte ao cuidador se a criança usa pelo menos 6 palavras diferentes "
        "com significado. Critério (Passa): cuidador confirma esse vocabulário.",
    ),
    (
        18,
        "Aponta 2 figuras",
        15,
        22,
        False,
        "Procedimento: apresente uma prancha com várias figuras e peça que a "
        "criança aponte 2 delas, nomeadas pelo examinador. Critério (Passa): a "
        "criança aponta corretamente as 2 figuras solicitadas.",
    ),
    (
        19,
        "Combina palavras (An)",
        16,
        24,
        True,
        "Pergunte ao cuidador se a criança combina duas palavras em uma frase "
        "curta (ex.: 'quer água'). Critério (Passa): cuidador confirma "
        "combinações de palavras.",
    ),
    (
        20,
        "Nomeia 1 figura",
        17,
        23,
        False,
        "Procedimento: apresente uma figura simples e pergunte 'o que é isso?'. "
        "Critério (Passa): a criança nomeia corretamente ao menos 1 figura.",
    ),
    (
        21,
        "Aponta 6 partes do corpo",
        18,
        24,
        False,
        "Procedimento: peça que a criança aponte partes do próprio corpo ou de "
        "uma boneca (nariz, olhos, boca etc.). Critério (Passa): a criança aponta "
        "corretamente ao menos 6 partes do corpo.",
    ),
    (
        22,
        "Aponta 4 figuras",
        19,
        25,
        False,
        "Procedimento: apresente uma prancha com figuras e peça que a criança "
        "aponte 4 delas, nomeadas pelo examinador. Critério (Passa): a criança "
        "aponta corretamente ao menos 4 figuras.",
    ),
    (
        23,
        "50% de inteligibilidade de fala",
        20,
        31,
        False,
        "Procedimento: observe a fala espontânea da criança durante a avaliação. "
        "Critério (Passa): cerca de metade da fala é compreensível para um "
        "interlocutor não familiarizado.",
    ),
    (
        24,
        "Nomeia 4 figuras",
        21,
        31,
        False,
        "Procedimento: apresente uma prancha com figuras e peça que a criança "
        "nomeie cada uma. Critério (Passa): a criança nomeia corretamente ao "
        "menos 4 figuras.",
    ),
    (
        25,
        "Reconhece 2 ações",
        22,
        32,
        False,
        "Procedimento: apresente figuras de ações (ex.: correndo, comendo) e "
        "pergunte 'quem está...?'. Critério (Passa): a criança reconhece "
        "corretamente ao menos 2 ações.",
    ),
    (
        26,
        "Compreende 2 adjetivos",
        24,
        36,
        False,
        "Procedimento: peça que a criança execute comandos simples com adjetivos "
        "(ex.: 'me dê o lápis maior'). Critério (Passa): a criança responde "
        "corretamente a ao menos 2 comandos com adjetivos.",
    ),
    (
        27,
        "Nomeia 1 cor",
        26,
        40,
        False,
        "Procedimento: aponte para um objeto colorido e pergunte 'de que cor é "
        "isso?'. Critério (Passa): a criança nomeia corretamente ao menos 1 cor.",
    ),
    (
        28,
        "Define 2 objetos pelo uso",
        27,
        40,
        False,
        "Procedimento: pergunte 'para que serve...?' sobre objetos comuns. "
        "Critério (Passa): a criança descreve corretamente o uso de ao menos 2 "
        "objetos.",
    ),
    (
        29,
        "Conta 1 bloco",
        28,
        40,
        False,
        "Procedimento: peça que a criança conte um bloco entregue a ela ('me dê 1 "
        "bloco'). Critério (Passa): a criança entrega corretamente a quantidade "
        "solicitada.",
    ),
    (
        30,
        "Define 3 objetos pelo uso",
        30,
        44,
        False,
        "Procedimento: pergunte 'para que serve...?' sobre diferentes objetos "
        "comuns. Critério (Passa): a criança descreve corretamente o uso de ao "
        "menos 3 objetos.",
    ),
    (
        31,
        "Reconhece 4 ações",
        31,
        44,
        False,
        "Procedimento: apresente figuras de ações e pergunte 'quem está...?'. "
        "Critério (Passa): a criança reconhece corretamente ao menos 4 ações.",
    ),
    (
        32,
        "Fala inteligível",
        32,
        46,
        False,
        "Procedimento: observe a fala espontânea da criança durante toda a "
        "avaliação. Critério (Passa): a fala é totalmente compreensível para um "
        "interlocutor não familiarizado.",
    ),
    (
        33,
        "Compreende 4 preposições",
        34,
        48,
        False,
        "Procedimento: peça que a criança coloque um objeto em relação a outro "
        "(em cima, embaixo, atrás, na frente). Critério (Passa): a criança "
        "executa corretamente ao menos 4 comandos com preposições.",
    ),
    (
        34,
        "Nomeia 4 cores",
        37,
        52,
        False,
        "Procedimento: aponte objetos/figuras de cores diferentes e pergunte a "
        "cor de cada um. Critério (Passa): a criança nomeia corretamente ao "
        "menos 4 cores.",
    ),
    (
        35,
        "Define 5 palavras",
        39,
        55,
        False,
        "Procedimento: pergunte 'o que é...?' sobre palavras comuns. Critério "
        "(Passa): a criança define corretamente ao menos 5 palavras.",
    ),
    (
        36,
        "Compreende 3 adjetivos",
        40,
        55,
        False,
        "Procedimento: peça que a criança execute comandos com diferentes "
        "adjetivos (ex.: 'me dê o objeto mais leve'). Critério (Passa): a "
        "criança responde corretamente a ao menos 3 comandos.",
    ),
    (
        37,
        "Conta 5 blocos",
        42,
        58,
        False,
        "Procedimento: peça que a criança conte e entregue 5 blocos. Critério "
        "(Passa): a criança conta corretamente até 5 blocos.",
    ),
    (
        38,
        "Faz analogias - 2",
        45,
        60,
        False,
        "Procedimento: apresente analogias verbais simples (ex.: 'gelo é frio, "
        "fogo é...'). Critério (Passa): a criança completa corretamente ao "
        "menos 2 analogias.",
    ),
    (
        39,
        "Define 7 palavras",
        48,
        65,
        False,
        "Procedimento: pergunte 'o que é...?' sobre palavras comuns e abstratas. "
        "Critério (Passa): a criança define corretamente ao menos 7 palavras.",
    ),
]

LA_ITEMS: list[dict] = _build("LA", "la", _LA_ROWS)


# ---------------------------------------------------------------------------
# Motor Grosso (MG) — 32 itens
# ---------------------------------------------------------------------------
_MG_ROWS: list[ItemRow] = [
    (
        1,
        "Movimentos simétricos",
        0,
        1,
        False,
        "Procedimento: observe os movimentos espontâneos dos membros do bebê em "
        "decúbito dorsal. Critério (Passa): os movimentos dos braços e pernas "
        "são simétricos entre os lados.",
    ),
    (
        2,
        "Eleva a cabeça (An)",
        0,
        2,
        True,
        "Pergunte ao cuidador se, em posição de bruços, o bebê eleva a cabeça "
        "brevemente. Critério (Passa): cuidador confirma a elevação da cabeça.",
    ),
    (
        3,
        "Mantém a cabeça a 45°",
        1,
        3,
        False,
        "Procedimento: coloque o bebê em decúbito ventral e observe. Critério "
        "(Passa): o bebê eleva e mantém a cabeça a cerca de 45° por alguns "
        "segundos.",
    ),
    (
        4,
        "Mantém a cabeça a 90°",
        2,
        4,
        False,
        "Procedimento: coloque o bebê em decúbito ventral e observe. Critério "
        "(Passa): o bebê eleva e mantém a cabeça a cerca de 90°, sustentando-se "
        "nos antebraços.",
    ),
    (
        5,
        "Sentada, sustenta a cabeça",
        2,
        4,
        False,
        "Procedimento: segure o bebê sentado, apoiando o tronco. Critério "
        "(Passa): o bebê mantém a cabeça firme e ereta, sem cair para os lados.",
    ),
    (
        6,
        "Sustenta seu peso nas pernas",
        3,
        5,
        False,
        "Procedimento: segure o bebê em pé, com apoio, sobre uma superfície "
        "firme. Critério (Passa): o bebê sustenta parte do peso nas pernas, sem "
        "dobrar os joelhos completamente.",
    ),
    (
        7,
        "Eleva o peito",
        3,
        5,
        False,
        "Procedimento: coloque o bebê em decúbito ventral e observe. Critério "
        "(Passa): o bebê eleva o peito apoiando-se nas mãos/antebraços estendidos.",
    ),
    (
        8,
        "Muda de posição",
        4,
        7,
        False,
        "Procedimento: observe o bebê livre em uma superfície segura. Critério "
        "(Passa): o bebê rola de barriga para as costas ou vice-versa.",
    ),
    (
        9,
        "Puxada para sentar-se, mantém a cabeça firme",
        4,
        7,
        False,
        "Procedimento: segure as mãos do bebê e puxe-o suavemente para a posição "
        "sentada. Critério (Passa): a cabeça acompanha o movimento do tronco, "
        "sem cair para trás.",
    ),
    (
        10,
        "Senta sem apoio",
        6,
        9,
        False,
        "Procedimento: coloque o bebê sentado sem apoio das mãos do examinador. "
        "Critério (Passa): o bebê permanece sentado sozinho por alguns segundos.",
    ),
    (
        11,
        "De pé, sustenta o corpo",
        6,
        10,
        False,
        "Procedimento: segure o bebê em pé com apoio leve. Critério (Passa): o "
        "bebê sustenta o peso do corpo firmemente sobre as pernas estendidas.",
    ),
    (
        12,
        "Puxa para levantar-se",
        8,
        12,
        False,
        "Procedimento: observe a criança próxima a um móvel estável. Critério "
        "(Passa): a criança se puxa para ficar em pé segurando-se em um apoio.",
    ),
    (
        13,
        "Senta-se",
        8,
        12,
        False,
        "Procedimento: observe a criança em posição livre, sem auxílio. Critério "
        "(Passa): a criança consegue sentar-se sozinha a partir de outra posição.",
    ),
    (
        14,
        "Fica de pé",
        9,
        13,
        False,
        "Procedimento: observe a criança próxima a um apoio. Critério (Passa): a "
        "criança fica em pé segurando-se em um móvel/apoio, por alguns segundos.",
    ),
    (
        15,
        "Fica de pé sozinha",
        10,
        14,
        False,
        "Procedimento: observe a criança sem oferecer apoio. Critério (Passa): a "
        "criança permanece em pé sozinha por alguns segundos, sem se apoiar.",
    ),
    (
        16,
        "Abaixa-se e levanta-se",
        11,
        15,
        False,
        "Procedimento: coloque um objeto no chão e observe a criança pegá-lo. "
        "Critério (Passa): a criança se abaixa para pegar o objeto e volta a "
        "ficar em pé sozinha.",
    ),
    (
        17,
        "Anda bem",
        11,
        15,
        False,
        "Procedimento: observe a marcha independente da criança em superfície "
        "plana. Critério (Passa): a criança anda sozinha com boa estabilidade, "
        "sem apoio.",
    ),
    (
        18,
        "Anda para trás (An)",
        14,
        20,
        True,
        "Pergunte ao cuidador se a criança consegue andar alguns passos para "
        "trás. Critério (Passa): cuidador confirma essa habilidade.",
    ),
    (
        19,
        "Corre",
        15,
        21,
        False,
        "Procedimento: observe a criança em espaço livre, incentivando-a a "
        "correr. Critério (Passa): a criança corre com coordenação, sem cair "
        "com frequência.",
    ),
    (
        20,
        "Sobe escada (An)",
        15,
        22,
        True,
        "Pergunte ao cuidador se a criança sobe escadas, mesmo que com apoio, "
        "alternando ou não os pés. Critério (Passa): cuidador confirma essa "
        "habilidade.",
    ),
    (
        21,
        "Chuta bola",
        18,
        24,
        False,
        "Procedimento: coloque uma bola no chão e peça que a criança a chute. "
        "Critério (Passa): a criança chuta a bola sem perder o equilíbrio ao "
        "ponto de cair.",
    ),
    (
        22,
        "Pula",
        21,
        28,
        False,
        "Procedimento: demonstre pular com os dois pés e peça que a criança "
        "repita. Critério (Passa): a criança pula com os dois pés saindo do "
        "chão simultaneamente.",
    ),
    (
        23,
        "Arremessa bola",
        23,
        33,
        False,
        "Procedimento: peça que a criança arremesse uma bola em direção ao "
        "examinador, por cima do ombro. Critério (Passa): a criança arremessa a "
        "bola de forma dirigida.",
    ),
    (
        24,
        "Salta",
        26,
        36,
        False,
        "Procedimento: demonstre saltar para frente com os dois pés juntos e "
        "peça que a criança repita. Critério (Passa): a criança salta para "
        "frente sem cair.",
    ),
    (
        25,
        "Equilibra-se em cada pé por 1 segundo",
        28,
        40,
        False,
        "Procedimento: peça que a criança fique em um pé só, sem apoio, "
        "cronometrando o tempo. Critério (Passa): a criança mantém o equilíbrio "
        "em cada pé por pelo menos 1 segundo.",
    ),
    (
        26,
        "Equilibra-se em cada pé por 2 segundos",
        31,
        44,
        False,
        "Procedimento: peça que a criança fique em um pé só, sem apoio, "
        "cronometrando o tempo. Critério (Passa): a criança mantém o equilíbrio "
        "em cada pé por pelo menos 2 segundos.",
    ),
    (
        27,
        "Pula em um pé só",
        33,
        48,
        False,
        "Procedimento: demonstre pular em um pé só e peça que a criança repita. "
        "Critério (Passa): a criança pula ao menos uma vez em um pé só, "
        "mantendo o equilíbrio.",
    ),
    (
        28,
        "Equilibra-se em cada pé por 3 segundos",
        36,
        50,
        False,
        "Procedimento: peça que a criança fique em um pé só, sem apoio, "
        "cronometrando o tempo. Critério (Passa): a criança mantém o equilíbrio "
        "em cada pé por pelo menos 3 segundos.",
    ),
    (
        29,
        "Equilibra-se em cada pé por 4 segundos",
        39,
        54,
        False,
        "Procedimento: peça que a criança fique em um pé só, sem apoio, "
        "cronometrando o tempo. Critério (Passa): a criança mantém o equilíbrio "
        "em cada pé por pelo menos 4 segundos.",
    ),
    (
        30,
        "Equilibra-se em cada pé por 5 segundos",
        42,
        58,
        False,
        "Procedimento: peça que a criança fique em um pé só, sem apoio, "
        "cronometrando o tempo. Critério (Passa): a criança mantém o equilíbrio "
        "em cada pé por pelo menos 5 segundos.",
    ),
    (
        31,
        "Marcha ponta-calcanhar",
        45,
        60,
        False,
        "Procedimento: demonstre andar em linha reta encostando o calcanhar na "
        "ponta do pé oposto a cada passo e peça que a criança repita. Critério "
        "(Passa): a criança executa a marcha ponta-calcanhar por alguns passos "
        "com equilíbrio.",
    ),
    (
        32,
        "Equilibra-se em cada pé por 6 segundos",
        48,
        65,
        False,
        "Procedimento: peça que a criança fique em um pé só, sem apoio, "
        "cronometrando o tempo. Critério (Passa): a criança mantém o equilíbrio "
        "em cada pé por pelo menos 6 segundos.",
    ),
]

MG_ITEMS: list[dict] = _build("MG", "mg", _MG_ROWS)


ALL_MODULES: dict[str, list[dict]] = {
    "pessoal-social": PS_ITEMS,
    "motor-fino": FM_ITEMS,
    "linguagem": LA_ITEMS,
    "motor-grosso": MG_ITEMS,
}


if __name__ == "__main__":
    for slug, items in ALL_MODULES.items():
        print(f"{slug}: {len(items)} itens")
