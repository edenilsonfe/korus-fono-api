from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator

from app.schemas.common import CamelModel


TaskStatus = Literal["todo", "doing", "done"]


class PersonalTaskCreate(CamelModel):
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    due_date: date | None = None
    patient_id: UUID | None = None

    @field_validator("title")
    @classmethod
    def clean_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Informe o título da tarefa")
        return value

    @field_validator("description")
    @classmethod
    def clean_description(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None


class PersonalTaskUpdate(CamelModel):
    title: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    due_date: date | None = None
    patient_id: UUID | None = None
    status: TaskStatus | None = None

    @field_validator("title")
    @classmethod
    def clean_title(cls, value: str | None) -> str:
        if value is None or not value.strip():
            raise ValueError("Informe o título da tarefa")
        return value.strip()

    @field_validator("description")
    @classmethod
    def clean_description(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None

    @field_validator("status")
    @classmethod
    def require_status(cls, value: TaskStatus | None) -> TaskStatus:
        if value is None:
            raise ValueError("Informe o status da tarefa")
        return value


class PersonalTaskResponse(CamelModel):
    id: UUID
    title: str
    description: str | None
    due_date: date | None
    patient_id: UUID | None
    patient_name: str | None
    status: TaskStatus
    created_at: datetime
    updated_at: datetime
