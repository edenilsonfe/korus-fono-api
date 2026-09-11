"""Structured prompts and context specs for AI tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.schemas.ai import AIToolRequest

BASE_PERSONA = (
    "Você é fonoaudiólogo(a) experiente em terapia infantil, redator(a) clínico(a) do Korus Fono. "
    "Escreva em português (Brasil). Baseie-se exclusivamente no contexto clínico fornecido; "
    "nunca invente dados, resultados ou datas. Quando faltar informação, escreva 'não avaliado' "
    "ou omita a seção."
)


@dataclass(frozen=True)
class ToolSpec:
    system: str
    sections: list[str]
    limits: dict[str, int]
    prompt_template: str
    output: Literal["markdown", "plain"]


def _report_system(audience: str) -> str:
    return f"{BASE_PERSONA}\n\nPúblico-alvo do documento: {audience}."


AI_TOOL_SPECS: dict[str, ToolSpec] = {
    "report:clinico": ToolSpec(
        system=_report_system("profissional de saúde"),
        sections=["identity", "assessments", "evolutions", "goals", "anamnesis", "attendance"],
        limits={"evolutions": 8, "assessments": 5},
        prompt_template=(
            "Gere um relatório clínico detalhado com as seções obrigatórias em markdown "
            "(use ## para cada seção):\n"
            "## Identificação\n"
            "## Histórico clínico\n"
            "## Avaliações realizadas\n"
            "## Evolução terapêutica\n"
            "## Conduta e recomendações"
        ),
        output="markdown",
    ),
    "report:escolar": ToolSpec(
        system=_report_system("equipe pedagógica escolar"),
        sections=["identity", "evolutions", "goals", "assessments"],
        limits={"evolutions": 5, "assessments": 5},
        prompt_template=(
            "Gere um relatório escolar com orientações pedagógicas. "
            "Use ## para cada seção obrigatória:\n"
            "## Perfil do aluno\n"
            "## Impactos no contexto escolar\n"
            "## Orientações pedagógicas\n"
            "## Acompanhamento"
        ),
        output="markdown",
    ),
    "report:pais": ToolSpec(
        system=(
            f"{BASE_PERSONA}\n\n"
            "Público-alvo: pais e responsáveis. Use linguagem acessível, sem jargão técnico, "
            "tom acolhedor e empático."
        ),
        sections=["identity", "evolutions", "goals"],
        limits={"evolutions": 5},
        prompt_template=(
            "Gere um relatório para os pais/responsáveis. "
            "Use ## para cada seção obrigatória:\n"
            "## Como está o(a) paciente\n"
            "## Avanços recentes\n"
            "## Como ajudar em casa"
        ),
        output="markdown",
    ),
    "report:evolutivo": ToolSpec(
        system=_report_system("profissional de saúde — acompanhamento evolutivo"),
        sections=[
            "identity",
            "domain_snapshots",
            "evolutions",
            "goals",
            "assessments",
            "attendance",
        ],
        limits={"evolutions": 10, "assessments": 5},
        prompt_template=(
            "Gere um relatório evolutivo comparando progresso ao longo do tempo. "
            "Use ## para cada seção obrigatória:\n"
            "## Linha de base\n"
            "## Progresso por domínio\n"
            "## Progresso das metas\n"
            "## Comparativo do período\n"
            "## Próximos passos"
        ),
        output="markdown",
    ),
    "report:consolidado": ToolSpec(
        system=(
            f"{BASE_PERSONA}\n\n"
            "Público-alvo do documento: profissional de saúde (fonoaudiólogo responsável). "
            "Você redige apenas as seções 'Síntese' e 'Conduta' de um laudo consolidado "
            "multi-instrumento; a identificação e a lista de instrumentos já foram montadas "
            "deterministicamente fora do seu texto. Baseie-se somente no contexto fornecido: "
            "nunca invente normas, tabelas, escores, percentuais ou datas, e nunca compare "
            "percentuais de instrumentos diferentes como se fossem a mesma escala."
        ),
        sections=[],
        limits={},
        prompt_template=(
            "Redija a Síntese e a Conduta do laudo consolidado como rascunho para revisão da "
            "profissional. Use exatamente estas duas seções em markdown, nesta ordem e sem "
            "nenhum texto antes delas:\n"
            "## Síntese\n"
            "## Conduta\n"
            "Na Síntese você pode usar subtítulos para avaliações, evoluções, sessões, metas e "
            "comparativos selecionados. Em cada seção, use apenas os dados do contexto; para "
            "dados ausentes, informe que não foram registrados; não crie conteúdo clínico."
        ),
        output="markdown",
    ),
    "therapy-plan": ToolSpec(
        system=f"{BASE_PERSONA}\n\nElabore planos terapêuticos estruturados e revisões de planos existentes.",
        sections=[
            "identity",
            "assessments",
            "goals",
            "evolutions",
            "therapy_plan",
            "anamnesis",
        ],
        limits={"evolutions": 5, "assessments": 5},
        prompt_template=(
            "Monte ou revise um plano terapêutico trimestral. "
            "Se já houver plano no contexto, proponha revisão citando o que muda. "
            "Use ## para cada seção obrigatória:\n"
            "## Objetivos gerais\n"
            "## Objetivos específicos por área\n"
            "## Estratégias e atividades\n"
            "## Frequência e duração\n"
            "## Critérios de reavaliação"
        ),
        output="markdown",
    ),
    "suggest-goals": ToolSpec(
        system=f"{BASE_PERSONA}\n\nSugira metas SMART ancoradas em evidências clínicas do contexto.",
        sections=["identity", "goals", "assessments", "evolutions", "domain_snapshots"],
        limits={"evolutions": 5, "assessments": 5},
        prompt_template=(
            "Sugira metas terapêuticas SMART agrupadas por área. "
            "Cada meta deve ter justificativa ancorada em avaliação ou evolução citada. "
            "Não repita metas ativas já listadas no contexto. "
            "Use ## para cada área e bullets para as metas."
        ),
        output="markdown",
    ),
    "clinical-trends": ToolSpec(
        system=f"{BASE_PERSONA}\n\nAnalise tendências clínicas e sinalize estagnação ou regressão.",
        sections=["identity", "domain_snapshots", "evolutions", "goals", "attendance"],
        limits={"evolutions": 10},
        prompt_template=(
            "Identifique tendências clínicas do paciente. "
            "Use ## para cada seção obrigatória:\n"
            "## Tendências por domínio\n"
            "## Alertas (estagnação/regressão)\n"
            "## Hipóteses e recomendações"
        ),
        output="markdown",
    ),
    "session-summary": ToolSpec(
        system=(
            f"{BASE_PERSONA}\n\n"
            "Resuma a sessão em formato de evolução clínica com parágrafos curtos e objetivos."
        ),
        sections=[],
        limits={},
        prompt_template="Resuma a sessão a seguir em formato de evolução clínica:\n\n{input_text}",
        output="plain",
    ),
    "proofread": ToolSpec(
        system=(
            f"{BASE_PERSONA}\n\n"
            "Revise o texto clínico mantendo o sentido original. "
            "Retorne apenas o texto revisado, sem comentários nem preâmbulo."
        ),
        sections=[],
        limits={},
        prompt_template="Revise o texto clínico a seguir:\n\n{input_text}",
        output="plain",
    ),
}


def build_tool_prompt(
    spec: ToolSpec,
    *,
    context: str = "",
    extra_prompt: str | None = None,
    input_text: str | None = None,
) -> str:
    """Assemble the user prompt from a tool spec and runtime inputs."""
    if "{input_text}" in spec.prompt_template:
        prompt = spec.prompt_template.format(input_text=input_text or "")
    else:
        prompt = spec.prompt_template
        if context:
            prompt += f"\n\nContexto clínico:\n{context}"
    if extra_prompt:
        prompt += f"\n\nInstruções adicionais: {extra_prompt}"
    return prompt


def build_request_prompt(spec_key: str, body: AIToolRequest, context: str = "") -> tuple[ToolSpec, str]:
    """Resolve spec and build prompt for an API tool request."""
    spec = AI_TOOL_SPECS[spec_key]
    input_text = None
    if spec_key == "session-summary":
        input_text = body.session_notes or body.text or ""
    elif spec_key == "proofread":
        input_text = body.text or ""
    prompt = build_tool_prompt(
        spec,
        context=context,
        extra_prompt=body.prompt,
        input_text=input_text,
    )
    return spec, prompt
