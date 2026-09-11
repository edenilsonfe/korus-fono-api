"""Fixed catalog metadata for the resources library."""

RESOURCE_CATEGORIES: tuple[str, ...] = (
    # Área clínica
    "Linguagem", "Fala", "Voz", "Fluência", "Motricidade Orofacial",
    "Leitura e Escrita", "Audição", "TEA", "Comunicação Alternativa",
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

# F17 — estado editorial do recurso.
RESOURCE_PUBLICATION_STATUSES: tuple[str, ...] = ("draft", "published", "archived")

# F17 — licença de distribuição.
RESOURCE_LICENSE_STATUSES: tuple[str, ...] = (
    "declared",
    "pending",
    "approved",
    "rejected",
    "revoked",
)
RESOURCE_LICENSE_ORIGINS: tuple[str, ...] = ("original", "licensed", "public_domain")
RESOURCE_LICENSE_DECISIONS: tuple[str, ...] = ("approved", "rejected", "revoked")
RESOURCE_LICENSE_DECISION_REASON_MIN = 5
RESOURCE_LICENSE_DECISION_REASON_MAX = 500

RESOURCE_ALLOWED_CONTENT_TYPES: dict[str, str] = {
    "application/pdf": "PDF",
    "image/png": "Imagem",
    "image/jpeg": "Imagem",
}

RESOURCE_MAX_BYTES = 20 * 1024 * 1024
