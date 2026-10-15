"""API request / response models (validation at the edge)."""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    email: str
    name: str
    role: str
    is_active: bool
    created_at: datetime


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=1, max_length=200)


class Password(BaseModel):
    password: str = Field(..., min_length=10, max_length=128)

    @field_validator("password")
    @classmethod
    def strong(cls, v: str) -> str:
        if not any(c.isalpha() for c in v) or not any(c.isdigit() for c in v):
            raise ValueError("use at least 10 characters with letters and digits")
        return v


class RegisterIn(Password):
    email: EmailStr
    name: str = Field(..., min_length=1, max_length=100)


class PasswordChangeIn(Password):
    current_password: str = Field(..., min_length=1, max_length=200)


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut


class BriefCreate(BaseModel):
    topic: str | None = Field(None, max_length=1000)
    audience: str = Field("business readers", max_length=120)
    depth: Literal["quick", "standard", "deep"] = "standard"
    auto_approve: bool = False


class BriefUpdate(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=200)
    audience: str | None = Field(None, max_length=120)
    depth: Literal["quick", "standard", "deep"] | None = None
    auto_approve: bool | None = None


class MessageIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)


class PlanApproveIn(BaseModel):
    plan: list[str] | None = Field(None, max_length=6)
    title: str | None = Field(None, max_length=200)


class ExportIn(BaseModel):
    request: str | None = Field(None, max_length=500, description="free text, e.g. 'slides for my manager'")
    format: str | None = Field(None, max_length=20)
    scope: Literal["full", "summary", "section"] = "full"
    section: str = Field("", max_length=100)
    style: str = Field("", max_length=200)


class ApiTokenIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=60)


class AdminUserIn(RegisterIn):
    role: Literal["user", "admin"] = "user"


class AdminUserUpdate(BaseModel):
    is_active: bool | None = None
    role: Literal["user", "admin"] | None = None
