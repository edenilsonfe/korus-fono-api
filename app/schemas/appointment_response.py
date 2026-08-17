from typing import Literal

from pydantic import Field

from app.schemas.common import CamelModel


class AppointmentResponseTokenRequest(CamelModel):
    token: str = Field(min_length=32, max_length=2048)


class AppointmentAttendanceResponseRequest(AppointmentResponseTokenRequest):
    action: Literal["confirm", "cancel"]


class AppointmentAttendanceResponse(CamelModel):
    appointment_date: str
    appointment_time: str
    professional_name: str
    status: str
    can_respond: bool
