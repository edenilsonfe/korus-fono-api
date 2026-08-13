from datetime import date as DateType, time as TimeType

from app.schemas.common import CamelModel
from app.schemas.finance import AppointmentCompleteRequest, AppointmentCompleteResponse


class WeekdaySlot(CamelModel):
    weekday: int
    time: TimeType
    duration: int = 50


class AppointmentCreate(CamelModel):
    patient_id: str
    date: DateType
    time: TimeType
    type: str
    duration: int = 50
    status: str = "pendente"
    appointment_type: str = "avulso"
    frequency: str | None = None
    end_date: DateType | None = None
    weekdays: list[int] | None = None
    weekday_slots: list[WeekdaySlot] | None = None


class AppointmentUpdate(CamelModel):
    date: DateType | None = None
    time: TimeType | None = None
    type: str | None = None
    duration: int | None = None
    status: str | None = None


class AppointmentResponse(CamelModel):
    id: str
    patient_id: str
    patient: str
    date: str
    time: str
    type: str
    therapist: str
    duration: int
    status: str
    appointment_type: str = "avulso"
    series_id: str | None = None
    frequency: str | None = None
    end_date: str | None = None
    weekdays: list[int] | None = None
    weekday_slots: list[WeekdaySlot] | None = None


class AppointmentCreateResponse(AppointmentResponse):
    children_created: int = 0
