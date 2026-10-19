from datetime import date, datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

Veredito = Literal["verdadeiro", "enganoso", "falso", "fora_de_contexto", "nao_verificavel"]


class CheckCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Tetos generosos: o limite "de verdade" (2.048 / 5.000 caracteres depois da limpeza) e
    # as mensagens amigáveis ficam em url_guard e text_input.
    url: str | None = Field(default=None, max_length=4096)
    texto: str | None = Field(default=None, max_length=10_000)

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        if (self.url is None) == (self.texto is None):
            raise ValueError("envie um link ou um texto")
        return self


class AgencyCheckOut(BaseModel):
    agencia: str
    titulo: str
    veredito: str
    url: str
    data: str | None = None


class CheckOut(BaseModel):
    # id é o public_id aleatório; o id numérico do banco nunca sai da API.
    id: str
    tipo: Literal["link", "texto"]
    url: str | None
    domain: str | None
    title: str | None
    published_at: date | None
    texto: str | None
    veredito: Veredito
    rebaixado_por_confianca: bool
    confianca: int
    resumo: str
    pontos_suspeitos: list[str]
    contexto_faltante: str | None
    checagens_agencias: list[AgencyCheckOut]
    fontes_sugeridas: list[str]
    created_at: datetime
    # "Verdadeiro" sem agência que confirme (mostrado como "Ainda sem checagem").
    rebaixado_sem_agencia: bool = False
    # Checagens da busca que a análise não usou: "Checagens sobre assuntos relacionados".
    checagens_relacionadas: list[AgencyCheckOut] = Field(default_factory=list)
    cache: bool = False
    # Feita com uma versão anterior do prompt (a tela mostra um aviso discreto).
    analise_anterior: bool = False
    # "agencias": Gemini sem cota ou no limite; só o que as agências publicaram, sem IA
    # (selo neutro "Análise automática indisponível agora").
    modo: Literal["ia", "agencias"] = "ia"
    # Busca nas agências desta análise, gravada no momento da checagem. None = checagem antiga,
    # sem esse registro (não se inventa: a página mostra só o que está gravado).
    busca_agencias: Literal["ok", "falhou"] | None = None


class CheckSummaryOut(BaseModel):
    id: str
    domain: str
    title: str | None
    veredito: Veredito
    created_at: datetime
    # 0 com "nao_verificavel": a lista mostra o neutro "Ainda sem checagem".
    total_agencias: int = 0


class LatestChecksOut(BaseModel):
    items: list[CheckSummaryOut]
    next_cursor: str | None
