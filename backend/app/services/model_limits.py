"""Limites do nível gratuito do Gemini por modelo (AI Studio, 12/10/2026) e família do modelo.

Módulo puro (sem SDK nem app): o config.py usa para validar GEMINI_MODEL_LIMITS.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

Family = Literal["flash", "lite", "gemma"]


@dataclass(frozen=True)
class ModelLimits:
    rpm: int  # requisições por minuto
    rpd: int  # requisições por dia (dia do Pacífico)
    tpm: int | None = None  # tokens por minuto (só os Gemma têm teto relevante: 16K)


# Nível gratuito, por modelo (dado do usuário, AI Studio, 12/10/2026). Gemini 2 Flash, 2.5 Pro e
# 3.1 Pro: 0 (indisponíveis). 2.5-flash: 404 "não disponível para contas novas" (12/10).
DEFAULT_MODEL_LIMITS: Mapping[str, ModelLimits] = {
    "gemini-3.8-flash": ModelLimits(rpm=5, rpd=20),
    "gemini-3.7-flash": ModelLimits(rpm=5, rpd=20),
    "gemini-3.6-flash": ModelLimits(rpm=5, rpd=20),
    "gemini-3-flash-preview": ModelLimits(rpm=5, rpd=20),
    "gemini-2.5-flash": ModelLimits(rpm=5, rpd=20),
    "gemini-3.5-flash-lite": ModelLimits(rpm=15, rpd=500),
    "gemini-3.1-flash-lite": ModelLimits(rpm=15, rpd=500),
    "gemini-2.5-flash-lite": ModelLimits(rpm=10, rpd=20),
    "gemma-4-31b-it": ModelLimits(rpm=30, rpd=14400, tpm=16000),
    "gemma-4-26b-a4b-it": ModelLimits(rpm=30, rpd=14400, tpm=16000),
}

_MODEL = re.compile(r"[a-z0-9][a-z0-9.\-]{0,62}", re.ASCII)
_NUMBERS = re.compile(r"\d{1,7}", re.ASCII)


def parse_model_limits(raw: str) -> dict[str, ModelLimits]:
    """GEMINI_MODEL_LIMITS: "modelo=rpm/rpd[/tpm],..." por cima da tabela padrão."""
    limits = dict(DEFAULT_MODEL_LIMITS)
    for item in (part.strip() for part in raw.split(",")):
        if not item:
            continue
        name, _, values = item.partition("=")
        numbers = values.split("/")
        if (
            not _MODEL.fullmatch(name.strip())
            or len(numbers) not in (2, 3)
            or not all(_NUMBERS.fullmatch(n.strip()) for n in numbers)
        ):
            raise ValueError(f"GEMINI_MODEL_LIMITS inválido: {item!r} (use modelo=rpm/rpd[/tpm])")
        rpm, rpd, *tpm = (int(n) for n in numbers)
        limits[name.strip()] = ModelLimits(rpm=rpm, rpd=rpd, tpm=tpm[0] if tpm else None)
    return limits


def family(model: str) -> Family:
    """Cada família tem sua configuração (sondagem real de 12/10/2026): o Flash aceita
    thinking_budget=0; o Flash-Lite respondeu 400 com ele; o Gemma recusa thinking_budget e
    não teve JSON nativo nem system instruction confirmados."""
    if model.startswith("gemma-"):
        return "gemma"
    if "flash-lite" in model:
        return "lite"
    return "flash"
