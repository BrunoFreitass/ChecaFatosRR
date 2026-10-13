"""Configuração da chamada por família de modelo (Flash, Flash-Lite, Gemma) e por tarefa.

Sondagem real de 12/10/2026 (6 chamadas autorizadas):
- Flash (3.8, 3.7, 3.6): JSON nativo no schema + system instruction + thinking_budget=0.
- Flash-Lite: 400 com thinking_budget=0 → vai sem thinking_config.
- Gemma: 400 "Thinking budget is not supported"; JSON nativo e system instruction NÃO foram
  confirmados → instrução e schema no próprio pedido, JSON extraído da resposta e validado
  com Pydantic pelo chamador (que repete uma vez se vier fora do schema). O Gemma pensa antes
  de responder (509 tokens num comentário curto): teto de saída maior.
"""

import json
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from app.services.model_limits import (
    DEFAULT_MODEL_LIMITS,
    Family,
    ModelLimits,
    family,
    parse_model_limits,
)

__all__ = [
    "DEFAULT_MODEL_LIMITS",
    "Family",
    "ModelLimits",
    "TaskSpec",
    "build_task_config",
    "build_task_contents",
    "estimate_tokens",
    "extract_json",
    "family",
    "parse_model_limits",
]

# Gemma: o raciocínio também consome a saída; 512 tokens não bastaram (12/10/2026).
GEMMA_MIN_OUTPUT_TOKENS = 8192
_JSON_RULES = (
    "Responda SOMENTE com um objeto JSON válido, sem texto antes ou depois e sem ```, "
    "seguindo este JSON Schema:"
)


@dataclass(frozen=True)
class TaskSpec:
    instruction: str
    schema: type[BaseModel]
    temperature: float
    max_output_tokens: int
    thinking_budget: int = 0
    thinking_level: str = ""


def _check_task() -> TaskSpec:
    from app.services.gemini import SYSTEM_INSTRUCTION, GeminiVerdict

    return TaskSpec(
        instruction=SYSTEM_INSTRUCTION,
        schema=GeminiVerdict,
        temperature=0.2,
        max_output_tokens=4096,
    )


def __getattr__(name: str) -> Any:
    # CHECK_TASK depende do gemini.py, que importa este módulo dentro das funções: criado na
    # primeira leitura, sem import circular.
    if name == "CHECK_TASK":
        return _check_task()
    raise AttributeError(name)


def build_task_config(spec: TaskSpec, model: str) -> Any:
    from google.genai import types

    kind = family(model)
    if kind == "gemma":
        return types.GenerateContentConfig(
            temperature=spec.temperature,
            max_output_tokens=max(spec.max_output_tokens, GEMMA_MIN_OUTPUT_TOKENS),
        )
    thinking = None
    if kind == "flash":
        if spec.thinking_level:
            level = types.ThinkingLevel(spec.thinking_level.upper())
            thinking = types.ThinkingConfig(thinking_level=level)
        else:
            thinking = types.ThinkingConfig(thinking_budget=spec.thinking_budget)
    return types.GenerateContentConfig(
        system_instruction=spec.instruction,
        response_mime_type="application/json",
        response_schema=spec.schema,
        temperature=spec.temperature,
        max_output_tokens=spec.max_output_tokens,
        thinking_config=thinking,
    )


def build_task_contents(spec: TaskSpec, model: str, prompt: str) -> str:
    """O que vai em `contents`: o pedido; no Gemma, com a instrução e o schema na frente."""
    if family(model) != "gemma":
        return prompt
    schema = json.dumps(spec.schema.model_json_schema(), ensure_ascii=False)
    return f"{spec.instruction}\n\n{_JSON_RULES}\n{schema}\n\n{prompt}"


def extract_json(text: str) -> str:
    """O primeiro objeto JSON válido da resposta, mesmo dentro de ```json ... ``` ou com texto
    em volta (inclusive com chaves no texto, como um raciocínio que vazou)."""
    decoder = json.JSONDecoder()
    start = text.find("{")
    while start >= 0:
        try:
            obj, end = decoder.raw_decode(text, start)
        except ValueError:
            start = text.find("{", start + 1)
            continue
        if isinstance(obj, dict):
            return text[start:end]
        start = text.find("{", end)
    return text


def estimate_tokens(text: str) -> int:
    """Estimativa conservadora para o TPM (português: ~3,5 a 4 caracteres por token)."""
    return len(text) // 3 + 1
