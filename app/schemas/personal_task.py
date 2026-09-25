from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator

from app.schemas.common import CamelModel


TaskStatus = Literal["todo", "doing", "done"]
RepeatRule = Literal["none", "daily", "weekly", "monthly"]


class PersonalTaskColumn(CamelModel):
    id: str
    title: str


class PersonalTaskColumnCreate(CamelModel):
    title: str = Field(min_length=1, max_length=60)

    @field_validator("title")
    @classmethod
    def clean_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Informe o nome da coluna")
        return value


class PersonalTaskColumnOrder(CamelModel):
    column_ids: list[str] = Field(min_length=3, max_length=20)


class PersonalTaskChecklistItem(CamelModel):
    id: UUID
    title: str = Field(min_length=1, max_length=200)
    done: bool = False

    @field_validator("title")
    @classmethod
    def clean_item_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Informe o item do checklist")
        return value


class PersonalTaskCreate(CamelModel):
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    due_date: date | None = None
    patient_id: UUID | None = None
    appointment_id: UUID | None = None
    repeat_rule: RepeatRule = "none"
    remind_before_days: int | None = Field(default=None, ge=0, le=30)
    checklist: list[PersonalTaskChecklistItem] = Field(default_factory=list, max_length=30)
    column_key: str = Field(default="todo", min_length=1, max_length=36)

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
    appointment_id: UUID | None = None
    repeat_rule: RepeatRule | None = None
    remind_before_days: int | None = Field(default=None, ge=0, le=30)
    checklist: list[PersonalTaskChecklistItem] | None = Field(default=None, max_length=30)
    status: TaskStatus | None = None
    column_key: str | None = Field(default=None, min_length=1, max_length=36)

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

    @field_validator("repeat_rule", "checklist", "column_key")
    @classmethod
    def require_non_null(cls, value):
        if value is None:
            raise ValueError("Informe um valor válido")
        return value


class PersonalTaskResponse(CamelModel):
    id: UUID
    title: str
    description: str | None
    due_date: date | None
    patient_id: UUID | None
    patient_name: str | None
    appointment_id: UUID | None
    appointment_date: date | None
    repeat_rule: RepeatRule
    remind_before_days: int | None
    checklist: list[PersonalTaskChecklistItem]
    status: TaskStatus
    column_key: str
    created_at: datetime
    updated_at: datetime
