"""Decisão da moderação de um comentário: filtro local (camada 1) + IA (camada 2).

Função pura: quem chama grava o resultado (comentário, log, strike, fila). A IA só é
chamada para o que o filtro local deixou passar, então `decide(bloqueio_local, None)`.
"""

from dataclasses import dataclass
from typing import Literal

from app.services.moderation.local_filter import RULES as LOCAL_RULES
from app.services.moderation.local_filter import FilterResult

AiCategory = Literal[
    "ok",
    "palavrao",
    "ofensivo",
    "sexual",
    "incitacao_violencia",
    "crime",
    "discurso_de_odio",
    "desinformacao",
    "spam",
]
UnavailableReason = Literal["indisponivel", "sem_cota", "resposta_invalida"]
QueueReason = Literal["desinformacao", "nao_moderado_ia", "ai_low_confidence"]
Actor = Literal["local_filter", "ai_filter"]

# Strike automático só nestas (plano §9). As locais (palavrao, sexual, discurso_de_odio)
# estão todas aqui.
STRIKE_CATEGORIES: frozenset[str] = frozenset(
    {"palavrao", "sexual", "crime", "discurso_de_odio", "incitacao_violencia"}
)
# Bloqueio com aviso, sem strike (decisão de 12/10/2026); contam para o cooldown de 1 h.
BLOCK_WITHOUT_STRIKE: frozenset[str] = frozenset({"ofensivo", "spam"})
# Abaixo disso, o strike da IA espera o admin confirmar.
AI_STRIKE_MIN_CONFIDENCE = 0.85

# Aviso de violação (regra, explicação) das categorias que só a IA usa. As do filtro local
# ficam em local_filter.RULES, que também valida as seções da wordlist.
AI_RULES: dict[str, tuple[str, str]] = {
    "crime": (
        "Incentivo a crime",
        "Não são permitidos incentivo, apologia ou instruções para crimes, incluindo crimes "
        "eleitorais (como compra de votos).",
    ),
    "incitacao_violencia": (
        "Incitação à violência",
        "Não são permitidas ameaças nem incentivo à violência contra qualquer pessoa ou grupo.",
    ),
    "ofensivo": (
        "Ofensa pessoal",
        "Critique ideias, não pessoas: insultos e ataques pessoais não são permitidos.",
    ),
    "spam": (
        "Spam",
        "Não são permitidos propaganda, correntes, mensagens repetidas ou links sem relação "
        "com a conversa.",
    ),
    # Recusa de segurança do próprio modelo (resposta vazia): o admin decide se é strike.
    "conteudo_bloqueado": (
        "Conteúdo não permitido",
        "O comentário foi barrado pela verificação automática e vai passar por revisão.",
    ),
}
RULES: dict[str, tuple[str, str]] = {**LOCAL_RULES, **AI_RULES}


@dataclass(frozen=True)
class AiVerdict:
    categoria: AiCategory
    confianca: float  # 0 a 1


@dataclass(frozen=True)
class AiRefused:
    """O próprio modelo recusou o conteúdo (bloqueio de segurança do Gemini). Não é fail-open:
    é justamente o pior conteúdo. Bloqueia, sem strike, e o admin decide."""


@dataclass(frozen=True)
class AiUnavailable:
    """A IA não respondeu a tempo, ficou sem cota ou respondeu fora do schema."""

    reason: UnavailableReason


@dataclass(frozen=True)
class Decision:
    publish: bool
    actor: Actor | None = None  # quem bloqueou ou marcou (None = publicado sem ressalva)
    category: str | None = None
    strike: bool = False
    queue: QueueReason | None = None
    confidence: float | None = None

    @property
    def rule(self) -> str | None:
        return RULES[self.category][0] if not self.publish and self.category else None

    @property
    def explanation(self) -> str | None:
        return RULES[self.category][1] if not self.publish and self.category else None

    @property
    def counts_for_cooldown(self) -> bool:
        return not self.publish and self.category in BLOCK_WITHOUT_STRIKE


def decide(local: FilterResult, ai: AiVerdict | AiRefused | AiUnavailable | None) -> Decision:
    if local.blocked:
        return Decision(
            publish=False,
            actor="local_filter",
            category=local.category,
            strike=local.category in STRIKE_CATEGORIES,
        )
    if ai is None:
        raise ValueError("texto aprovado pelo filtro local precisa da resposta da IA")
    if isinstance(ai, AiRefused):
        return Decision(
            publish=False,
            actor="ai_filter",
            category="conteudo_bloqueado",
            queue="ai_low_confidence",
        )
    if isinstance(ai, AiUnavailable):
        # Fail-open: publica, e o admin revisa depois.
        return Decision(publish=True, queue="nao_moderado_ia")
    if ai.categoria == "ok":
        return Decision(publish=True)
    if ai.categoria == "desinformacao":
        return Decision(
            publish=True,
            actor="ai_filter",
            category="desinformacao",
            queue="desinformacao",
            confidence=ai.confianca,
        )
    if ai.categoria in BLOCK_WITHOUT_STRIKE:
        return Decision(
            publish=False, actor="ai_filter", category=ai.categoria, confidence=ai.confianca
        )
    sure = ai.confianca >= AI_STRIKE_MIN_CONFIDENCE
    return Decision(
        publish=False,
        actor="ai_filter",
        category=ai.categoria,
        strike=sure,
        queue=None if sure else "ai_low_confidence",
        confidence=ai.confianca,
    )
