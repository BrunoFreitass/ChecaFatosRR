"""Resposta de GET /admin/painel: só números agregados (nada por pessoa)."""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel


class ChecagensDia(BaseModel):
    dia: date
    total: int
    verdadeiro: int
    enganoso: int
    falso: int
    fora_de_contexto: int
    nao_verificavel: int
    so_agencias: int  # Gemini indisponível: só o que as agências publicaram


class TokensDia(BaseModel):
    dia: date
    checagens: int  # checagens com tokens gravados (as anteriores a 14/10/2026 não têm)
    entrada: int
    saida: int
    raciocinio: int


class PedidosDia(BaseModel):
    dia: date
    total: int
    do_cache: int


class ComentariosDia(BaseModel):
    dia: date
    total: int
    # Removidos pela moderação ou escondidos por denúncias (não os apagados pelo próprio autor).
    ocultos: int


class TotalDia(BaseModel):
    dia: date
    total: int


class Fila(BaseModel):
    pendentes: int
    revisao: int
    confirmacao_strike: int


class Usuarios(BaseModel):
    total: int
    suspensos: int
    banidos: int


class Cota(BaseModel):
    uso: Literal["checagem", "moderacao"]
    usadas: int
    limite: int
    restante: int


class Modelo(BaseModel):
    modelo: str
    usadas: int
    limite: int  # RPD do nível gratuito (ou GEMINI_MODEL_LIMITS)
    restante: int
    checagem: bool  # está na cadeia da checagem
    moderacao: bool  # está na cadeia da moderação


class Gemini(BaseModel):
    dia_pacifico: date  # a cota do Google vira à meia-noite do Pacífico
    cota: list[Cota]
    modelos: list[Modelo]


class PainelOut(BaseModel):
    gerado_em: datetime
    checagens: list[ChecagensDia]
    tokens: list[TokensDia]
    pedidos: list[PedidosDia]
    comentarios: list[ComentariosDia]
    denuncias: list[TotalDia]
    usuarios_novos: list[TotalDia]
    fila: Fila
    usuarios: Usuarios
    gemini: Gemini
