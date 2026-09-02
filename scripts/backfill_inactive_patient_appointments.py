"""Preview or cancel future appointments that belong to inactive patients.

Preview is read-only. Apply requires the exact aggregate counts and SHA-256
manifest returned by the reviewed preview.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

PUBLIC_DATABASE_URL = os.environ.get("DATABASE_PUBLIC_URL", "").strip()
if PUBLIC_DATABASE_URL:
    os.environ["DATABASE_URL"] = PUBLIC_DATABASE_URL

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.db.session import AsyncSessionLocal, engine  # noqa: E402
from app.services.patient_appointment_service import (  # noqa: E402
    InactivePatientAppointmentInventory,
    backfill_inactive_patient_appointments,
    get_inactive_patient_appointment_inventory,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Cancela agendamentos futuros pendentes ou confirmados de pacientes "
            "inativos. Sem --apply, apenas exibe contagens agregadas."
        )
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-environment")
    parser.add_argument("--expected-patient-count", type=int)
    parser.add_argument("--expected-appointment-count", type=int)
    parser.add_argument("--expected-sha256")
    return parser


def _payload(
    inventory: InactivePatientAppointmentInventory,
    *,
    mode: str,
    event_logs: int = 0,
    google_records: int = 0,
    verified: bool = False,
) -> dict:
    return {
        "mode": mode,
        **asdict(inventory),
        "whatsapp_event_logs_queued": event_logs,
        "google_sync_records_queued": google_records,
        "verified_idempotent": verified,
    }


def _validate_environment(expected_environment: str | None) -> None:
    if not expected_environment:
        return
    actual_environment = os.environ.get("RAILWAY_ENVIRONMENT_NAME", "").strip()
    if actual_environment != expected_environment:
        raise RuntimeError(
            "Ambiente Railway diferente do esperado: "
            f"esperado {expected_environment!r}, encontrado {actual_environment!r}"
        )


async def run(args: argparse.Namespace) -> int:
    _validate_environment(args.expected_environment)
    engine.echo = False
    event_logs = []
    google_records = []
    async with AsyncSessionLocal() as db:
        if args.apply:
            inventory, event_logs, google_records = (
                await backfill_inactive_patient_appointments(
                    db,
                    expected_patient_count=args.expected_patient_count,
                    expected_appointment_count=args.expected_appointment_count,
                    expected_manifest_sha256=args.expected_sha256,
                )
            )
            await db.commit()
        else:
            inventory = await get_inactive_patient_appointment_inventory(db)
            await db.rollback()

    verified = False
    if args.apply:
        async with AsyncSessionLocal() as verification_db:
            remaining = await get_inactive_patient_appointment_inventory(verification_db)
            await verification_db.rollback()
        verified = remaining.appointment_count == 0
        if not verified:
            raise RuntimeError(
                "A verificação pós-commit encontrou agendamentos ainda elegíveis"
            )

    print(
        json.dumps(
            _payload(
                inventory,
                mode="apply" if args.apply else "preview",
                event_logs=len(event_logs),
                google_records=len(google_records),
                verified=verified,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    required_apply_guards = {
        "--expected-environment": args.expected_environment,
        "--expected-patient-count": args.expected_patient_count,
        "--expected-appointment-count": args.expected_appointment_count,
        "--expected-sha256": args.expected_sha256,
    }
    missing = [name for name, value in required_apply_guards.items() if value is None]
    if args.apply and missing:
        print(
            "Aplicação bloqueada: informe " + ", ".join(missing) + ".",
            file=sys.stderr,
        )
        return 2
    try:
        return asyncio.run(run(args))
    except RuntimeError as exc:
        print(f"Aplicação bloqueada: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - do not expose clinical data
        print(
            f"Falha inesperada ({type(exc).__name__}); nenhum dado clínico foi exibido.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
