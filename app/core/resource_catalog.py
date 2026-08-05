"""Fixed catalog metadata for the resources library."""

RESOURCE_CATEGORIES: tuple[str, ...] = (
    # Área clínica
    "Linguagem", "Fala", "Voz", "Fluência", "Motricidade Orofacial",
    "Leitura e Escrita", "Audição", "Comunicação Alternativa",
    # Tipo de material
    "Jogos e Atividades", "Cartões e Figuras", "Guias e Orientações",
    "Avaliação", "Quadros e Cartazes",
    # Público
    "Orientação aos Pais",
)

RESOURCE_FORMATS: tuple[str, ...] = ("PDF", "Imagem")

RESOURCE_DIFFICULTIES: tuple[str, ...] = ("Básico", "Intermediário", "Avançado")

RESOURCE_ACCENTS: tuple[str, ...] = (
    "primary",
    "info",
    "success",
    "warning",
    "destructive",
)

RESOURCE_ALLOWED_CONTENT_TYPES: dict[str, str] = {
    "application/pdf": "PDF",
    "image/png": "Imagem",
    "image/jpeg": "Imagem",
}

RESOURCE_MAX_BYTES = 20 * 1024 * 1024
