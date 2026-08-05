"""Seed data for the global resources catalog (PDF/Imagem only)."""

from __future__ import annotations

# Minimal valid PDF and 1x1 PNG for object storage seeding.
SEED_PDF_BYTES = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Count 1/Kids[3 0 R]>>endobj\n"
    b"3 0 obj<</Type/Page/MediaBox[0 0 300 144]/Parent 2 0 R>>endobj\n"
    b"xref\n0 4\n0000000000 65535 f \n0000000009 00000 n \n0000000052 00000 n \n"
    b"0000000101 00000 n \ntrailer<</Size 4/Root 1 0 R>>\nstartxref\n178\n%%EOF"
)

SEED_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
    b"\x01\x01\x01\x00\x18\xdd\x8d\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)

GLOBAL_RESOURCE_SEED: list[dict] = [
    {
        "title": "Pranchas de Comunicação Alternativa (CAA)",
        "description": (
            "Conjunto de pranchas PECS imprimíveis com pictogramas de rotina, alimentação e emoções "
            "para crianças não verbais."
        ),
        "categories": ["Comunicação Alternativa", "TEA"],
        "format": "PDF",
        "filename": "pranchas-caa.pdf",
        "content_type": "application/pdf",
        "file_bytes": SEED_PDF_BYTES,
        "pages": 42,
        "author": "Dra. Camila Rocha",
        "downloads": 1284,
        "featured": True,
        "accent": "primary",
        "objective": "Ampliar a comunicação funcional de crianças não verbais",
        "age_range": "2–8 anos",
        "skill": "Comunicação expressiva",
        "related_protocol": "PECS",
        "difficulty": "Intermediário",
    },
    {
        "title": "Caderno de Estimulação de Linguagem (3–5 anos)",
        "description": (
            "Atividades lúdicas para ampliação de vocabulário, consciência fonológica e "
            "estruturação de frases."
        ),
        "categories": ["Linguagem", "Jogos e Atividades"],
        "format": "PDF",
        "filename": "caderno-linguagem-3-5.pdf",
        "content_type": "application/pdf",
        "file_bytes": SEED_PDF_BYTES,
        "pages": 68,
        "author": "Equipe KorusFono",
        "downloads": 982,
        "featured": True,
        "accent": "info",
        "objective": "Ampliar vocabulário e estruturação de frases",
        "age_range": "3–5 anos",
        "skill": "Linguagem oral",
        "related_protocol": "ABFW — Fonologia",
        "difficulty": "Básico",
    },
    {
        "title": "Guia de Integração Sensorial para Casa",
        "description": (
            "Orientações para famílias sobre atividades de regulação sensorial no ambiente domiciliar."
        ),
        "categories": ["Orientação aos Pais", "TEA"],
        "format": "PDF",
        "filename": "guia-integracao-sensorial.pdf",
        "content_type": "application/pdf",
        "file_bytes": SEED_PDF_BYTES,
        "pages": 24,
        "author": "TO. Marina Alves",
        "downloads": 631,
        "accent": "warning",
        "objective": "Orientar a regulação sensorial no ambiente domiciliar",
        "age_range": "2–10 anos",
        "skill": "Regulação sensorial",
        "related_protocol": "Perfil Sensorial 2",
        "difficulty": "Intermediário",
    },
    {
        "title": "Cartões de Praxias Orofaciais",
        "description": (
            "Cartões ilustrados para treino de movimentos de lábios, língua e bochechas em terapia "
            "miofuncional."
        ),
        "categories": ["Motricidade Orofacial", "Cartões e Figuras"],
        "format": "PDF",
        "filename": "cartoes-praxias-orofaciais.pdf",
        "content_type": "application/pdf",
        "file_bytes": SEED_PDF_BYTES,
        "pages": 30,
        "author": "Equipe KorusFono",
        "downloads": 873,
        "accent": "success",
        "objective": "Treinar mobilidade e praxias orofaciais",
        "age_range": "4–12 anos",
        "skill": "Motricidade orofacial",
        "related_protocol": "MBGR",
        "difficulty": "Avançado",
    },
    {
        "title": "Checklist de Marcos do Desenvolvimento (0–6 anos)",
        "description": (
            "Tabela de referência rápida de marcos de linguagem, motor, social e cognitivo para triagem."
        ),
        "categories": ["Avaliação", "Orientação aos Pais"],
        "format": "PDF",
        "filename": "checklist-marcos-desenvolvimento.pdf",
        "content_type": "application/pdf",
        "file_bytes": SEED_PDF_BYTES,
        "pages": 6,
        "author": "Dra. Camila Rocha",
        "downloads": 1502,
        "featured": True,
        "accent": "warning",
        "objective": "Rastrear marcos do desenvolvimento infantil",
        "age_range": "0–6 anos",
        "skill": "Rastreio do desenvolvimento",
        "related_protocol": "Denver II",
        "difficulty": "Básico",
    },
    {
        "title": "Folheto de Orientação aos Pais — Sinais de Alerta",
        "description": (
            "Material informativo para entregar às famílias sobre quando buscar avaliação especializada."
        ),
        "categories": ["Orientação aos Pais", "TEA", "Guias e Orientações"],
        "format": "PDF",
        "filename": "folheto-sinais-alerta.pdf",
        "content_type": "application/pdf",
        "file_bytes": SEED_PDF_BYTES,
        "pages": 4,
        "author": "Equipe KorusFono",
        "downloads": 1190,
        "accent": "info",
        "objective": "Informar famílias sobre sinais de alerta",
        "age_range": "Orientação aos pais",
        "skill": "Psicoeducação",
        "related_protocol": "M-CHAT-R",
        "difficulty": "Básico",
    },
    {
        "title": "Banco de Imagens para Nomeação",
        "description": (
            "Imagens categorizadas (objetos, ações, lugares) para terapia de linguagem expressiva."
        ),
        "categories": ["Linguagem", "Comunicação Alternativa", "Cartões e Figuras"],
        "format": "Imagem",
        "filename": "banco-imagens-nomeacao.png",
        "content_type": "image/png",
        "file_bytes": SEED_PNG_BYTES,
        "author": "Equipe KorusFono",
        "downloads": 967,
        "accent": "success",
        "objective": "Estimular linguagem expressiva por nomeação",
        "age_range": "2–10 anos",
        "skill": "Nomeação e vocabulário",
        "related_protocol": "ABFW — Vocabulário",
        "difficulty": "Básico",
    },
]
