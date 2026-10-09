from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ProfileCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Formato validado em app.services.nicknames (mensagem amigável); aqui só um teto.
    apelido: str = Field(max_length=40)
    aceito_termos: Literal[True]
    terms_version: str = Field(max_length=40)


class ProfileOut(BaseModel):
    apelido: str
    status: Literal["active", "suspended", "banned"]
    suspended_until: datetime | None
    is_admin: bool
    terms_version: str
    created_at: datetime


class NicknameAvailabilityOut(BaseModel):
    disponivel: bool
    motivo: Literal["formato", "reservado", "conteudo_proibido", "em_uso"] | None = None
