"""Preview or apply the supported legacy clinic SQL import.

Examples:
  uv run python scripts/import_legacy_clinic.py \
    --file backup.sql --professional-email profissional@example.com

  uv run python scripts/import_legacy_clinic.py \
    --file backup.sql --professional-email profissional@example.com \
    --apply --accept-inferred-state-map --expected-sha256 SHA256_DO_DRY_RUN
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.db.session import AsyncSessionLocal  # noqa: E402
from app.services.legacy_clinic_import import (  # noqa: E402
    LegacyClinicImportError,
    apply_legacy_clinic_import,
    preview_legacy_clinic_import,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Importa o backup SQL suportado da clínica antiga. "
            "O modo padrão é dry-run e não escreve no banco."
        )
    )
    parser.add_argument("--file", type=Path, required=True, help="Arquivo SQL de origem")
    parser.add_argument(
        "--professional-email",
        required=True,
        help="E-mail único da conta profissional de destino",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Aplica a importação em uma transação; sem esta opção, executa dry-run",
    )
    parser.add_argument(
        "--accept-inferred-state-map",
        action="store_true",
        help="Aceita explicitamente o mapeamento inferido dos estados da agenda",
    )
    parser.add_argument(
        "--expected-sha256",
        help="SHA-256 exibido no dry-run revisado",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        help=(
            "Manifesto local sem conteúdo clínico; por padrão usa "
            "<arquivo.sql>.korus-import.json ao lado do backup"
        ),
    )
    return parser


async def run(args: argparse.Namespace) -> int:
    async with AsyncSessionLocal() as db:
        if args.apply:
            result = await apply_legacy_clinic_import(
                db,
                source_path=args.file,
                professional_email=args.professional_email,
                accept_inferred_state_map=args.accept_inferred_state_map,
                expected_source_sha256=args.expected_sha256,
                manifest_path=args.manifest,
            )
        else:
            result = await preview_legacy_clinic_import(
                db,
                source_path=args.file,
                professional_email=args.professional_email,
            )
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(run(args))
    except LegacyClinicImportError as exc:
        print(f"Importação bloqueada: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - CLI must not leak clinical SQL parameters
        print(
            f"Falha inesperada na importação ({type(exc).__name__}); "
            "nenhum dado clínico foi exibido.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
