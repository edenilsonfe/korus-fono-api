from datetime import date, time

from pydantic import Field, model_validator

from app.schemas.common import CamelModel


class ScheduleBlockCreate(CamelModel):
    start_date: date
    end_date: date
    all_day: bool = True
    start_time: time | None = None
    end_time: time | None = None
    reason: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def validate_range(self):
        if self.end_date < self.start_date:
            raise ValueError("A data final deve ser igual ou posterior à data inicial")

        if self.all_day:
            self.start_time = None
            self.end_time = None
        elif self.start_time is None or self.end_time is None:
            raise ValueError("Informe os horários inicial e final")
        elif self.end_time <= self.start_time:
            raise ValueError("O horário final deve ser posterior ao horário inicial")

        if self.reason is not None:
            self.reason = self.reason.strip() or None
        return self


class ScheduleBlockResponse(CamelModel):
    id: str
    start_date: str
    end_date: str
    all_day: bool
    start_time: str | None = None
    end_time: str | None = None
    reason: str | None = None
