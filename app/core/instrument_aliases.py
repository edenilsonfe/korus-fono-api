"""Maps Korus protocol catalog IDs to myclinic instrument package slugs."""

PROTOCOL_TO_INSTRUMENT_SLUG: dict[str, str] = {
    "abfw": "abfw",
    "proc": "proc",
    "tvip": "tvip",
    "ados2": "ados-2",
    "cars": "cars",
    "vbmapp": "vb-mapp",
    "abllsr": "ablls-r",
    "denver2": "denver-ii",
    "bayley3": "bayley-iii",
    "pard": "pard",
    "fois": "fois",
    "adl": "adl",
    "adl-linguagem": "adl-linguagem",
    "doss": "doss",
    "mbgr": "mbgr",
    "amiofe": "amiofe",
    "omes": "omes",
}

MANIFEST_INSTRUMENT_SLUGS: frozenset[str] = frozenset(PROTOCOL_TO_INSTRUMENT_SLUG.values())

# Protocols scored on the client (afeto-clinic-manager configs)
CLIENT_SCORED_PROTOCOLS: frozenset[str] = frozenset(
    {
        "mchat",
        "snap-iv",
        "rastreio-tdah",
        "rastreio-tea",
        "dislexia",
        "sdq",
        "vanderbilt",
        "ata",
        "desenvolvimento-infantil",
        "habilidades-sociais",
        "desempenho-escolar",
        "portage",
    }
)

SPM_PROTOCOL = "spm"


def resolve_instrument_slug(protocol_id: str) -> str | None:
    return PROTOCOL_TO_INSTRUMENT_SLUG.get(protocol_id.lower())


def resolve_protocol_id(instrument_slug: str) -> str:
    """Map instrument package slug to protocol_catalog.id (FK on assessments)."""
    slug = instrument_slug.lower()
    for protocol_id, mapped_slug in PROTOCOL_TO_INSTRUMENT_SLUG.items():
        if mapped_slug == slug:
            return protocol_id
    return slug


def instrument_slug_for_protocol(protocol_id: str) -> str:
    """Map protocol_catalog.id (or slug) to instrument package slug."""
    return resolve_instrument_slug(protocol_id) or protocol_id.lower()


def has_manifest_package(protocol_id: str) -> bool:
    slug = resolve_instrument_slug(protocol_id)
    return slug is not None and slug in MANIFEST_INSTRUMENT_SLUGS
