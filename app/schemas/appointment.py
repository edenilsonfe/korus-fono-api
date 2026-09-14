from datetime import date as DateType, time as TimeType
from typing import Literal, TypeAlias
from uuid import UUID
from pydantic import Field, model_validator

from app.schemas.common import CamelModel
from app.schemas.finance import AppointmentCompleteRequest, AppointmentCompleteResponse


AppointmentStatus: TypeAlias = Literal[
    "pendente", "confirmado", "concluido", "cancelado", "falta"
]


class WeekdaySlot(CamelModel):
    weekday: int
    time: TimeType
    duration: int = 50


class AppointmentCreate(CamelModel):
    patient_id: str
    service_id: UUID | None = None
    date: DateType
    time: TimeType
    type: str
    duration: int = 50
    status: AppointmentStatus = "pendente"
    appointment_type: str = "avulso"
    frequency: str | None = None
    end_date: DateType | None = None
    weekdays: list[int] | None = None
    weekday_slots: list[WeekdaySlot] | None = None


class AppointmentUpdate(CamelModel):
    service_id: UUID | None = None
    date: DateType | None = None
    time: TimeType | None = None
    type: str | None = None
    duration: int | None = None
    status: AppointmentStatus | None = None


class AppointmentSeriesUpdate(CamelModel):
    from_date: DateType
    end_date: DateType
    frequency: Literal["semanal", "quinzenal", "mensal", "personalizado"]
    time: TimeType
    duration: int = Field(default=50, ge=1, le=1440)
    weekday_slots: list[WeekdaySlot] | None = Field(default=None, max_length=7)

    @model_validator(mode="after")
    def validate_schedule(self):
        from app.services.appointment_series_slots import validate_recurrent_range

        validate_recurrent_range(self.from_date, self.end_date)
        if self.frequency == "personalizado" and not self.weekday_slots:
            raise ValueError("Selecione ao menos um dia da semana")
        seen = set()
        for slot in self.weekday_slots or []:
            if slot.weekday not in range(7) or slot.weekday in seen:
                raise ValueError("Dias da semana inválidos ou duplicados")
            seen.add(slot.weekday)
        for slot_time, duration in [(self.time, self.duration)] + [
            (slot.time, slot.duration) for slot in self.weekday_slots or []
        ]:
            if slot_time.tzinfo is not None or not 1 <= duration <= 1440:
                raise ValueError("Horário ou duração inválidos")
            if slot_time.hour * 60 + slot_time.minute + slot_time.second / 60 + duration > 1440:
                raise ValueError("O atendimento deve terminar no mesmo dia")
        return self


class AppointmentSeriesUpdateResponse(CamelModel):
    created_count: int
    updated_count: int
    cancelled_count: int
    preserved_count: int


class AppointmentResponse(CamelModel):
    id: str
    patient_id: str
    patient: str
    service_id: str | None = None
    service_name: str | None = None
    service_price_cents: int | None = None
    date: str
    time: str
    type: str
    therapist: str
    duration: int
    status: AppointmentStatus
    appointment_type: str = "avulso"
    series_id: str | None = None
    frequency: str | None = None
    end_date: str | None = None
    weekdays: list[int] | None = None
    weekday_slots: list[WeekdaySlot] | None = None


class AppointmentCreateResponse(AppointmentResponse):
    children_created: int = 0
