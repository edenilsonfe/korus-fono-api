"""Preview or apply the canonical clinical history to every demo patient.

The default mode always rolls back. Applying requires the demo-patient count
observed in the reviewed preview.
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

from app.db.session import AsyncSessionLocal, engine  # noqa: E402
from app.services.demo_patient_backfill_service import (  # noqa: E402
    DemoPatientBackfillReport,
    enrich_all_demo_patients,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Enriquece pacientes de demonstração. O modo padrão é preview e "
            "não grava nenhuma alteração."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Confirma a gravação; sem esta opção, toda alteração é revertida",
    )
    parser.add_argument(
        "--expected-count",
        type=int,
        help="Contagem exata de pacientes demo exibida no preview revisado",
    )
    return parser


def _payload(
    report: DemoPatientBackfillReport,
    *,
    mode: str,
    verified: bool,
) -> dict:
    return {
        "mode": mode,
        **asdict(report),
        "total_records": report.total_records,
        "verified_idempotent": verified,
    }


async def run(args: argparse.Namespace) -> int:
    # Production may have SQL echo enabled; never print clinical row values here.
    engine.echo = False
    async with AsyncSessionLocal() as db:
        report = await enrich_all_demo_patients(
            db,
            expected_count=args.expected_count if args.apply else None,
        )
        if args.apply:
            await db.commit()
        else:
            await db.rollback()

    verified = False
    if args.apply:
        async with AsyncSessionLocal() as verification_db:
            verification = await enrich_all_demo_patients(
                verification_db,
                expected_count=args.expected_count,
            )
            await verification_db.rollback()
        verified = verification.total_records == 0
        if not verified:
            raise RuntimeError(
                "A verificação pós-commit encontrou registros demonstrativos pendentes"
            )

    print(
        json.dumps(
            _payload(
                report,
                mode="apply" if args.apply else "preview",
                verified=verified,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.apply and args.expected_count is None:
        print(
            "Aplicação bloqueada: informe --expected-count com a contagem do preview.",
            file=sys.stderr,
        )
        return 2
    try:
        return asyncio.run(run(args))
    except RuntimeError as exc:
        print(f"Aplicação bloqueada: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - CLI must not expose clinical data
        print(
            f"Falha inesperada ({type(exc).__name__}); nenhum dado clínico foi exibido.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
