from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class RegisterIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    role: Literal["teacher", "student"]


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    email: str
    role: str


class CourseIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=5000)


class JoinIn(BaseModel):
    code: str = Field(min_length=4, max_length=12)


class TopicIn(BaseModel):
    name: str = Field(min_length=1, max_length=160)


QuestionKind = Literal["single_choice", "multiple_choice", "matching", "short_text", "long_text"]


class QuestionIn(BaseModel):
    topic_id: int
    type: QuestionKind
    prompt: str = Field(min_length=1, max_length=10000)
    options: list[Any] = Field(default_factory=list)
    answer: Any = None
    rubric: str = ""
    verified: bool = False


class TestIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    duration_minutes: int = Field(ge=1, le=600)
    max_attempts: int = Field(ge=1, le=20)
    questions: list["TestQuestionIn"]


class TestQuestionIn(BaseModel):
    question_id: int = Field(gt=0)
    points: float = Field(default=1, gt=0)


class ProtectedModeConfirm(BaseModel):
    confirm_rules: bool


class AnswerIn(BaseModel):
    answer: Any


class EventIn(BaseModel):
    event_type: Literal["page_hidden", "focus_lost", "copy_attempt", "context_menu"]
    detail: str = Field(default="", max_length=250)


class GradeIn(BaseModel):
    score: float = Field(ge=0)
    feedback: str = Field(default="", max_length=5000)


class GenerateIn(BaseModel):
    material_id: int
    topic_id: int
    count: int = Field(ge=1, le=50)
    types: list[QuestionKind] = Field(min_length=1)
