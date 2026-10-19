"""Regras de veredito que valem na análise (finalize) E na leitura (página pública, Últimas).

Política de 10-11/10/2026: origem confirmada não é fato verificado. "Verdadeiro" exige uma
checagem de agência cuja classificação confirme o conteúdo; sem ela, "nao_verificavel"
("Ainda sem checagem") com um texto neutro no lugar do resumo do modelo. Aplicar também na
leitura corrige checagens salvas antes da regra, sem mexer no banco.
"""

import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

from app.services.factcheck import AgencyCheck
from app.services.relevance import is_relevant
from app.services.url_guard import URL_IN_TEXT, is_social_host

__all__ = ["corroborates", "is_open_platform", "neutral_summary", "read_verdict"]

# Lista FECHADA de rótulos (normalizados): "Verdadeiro, mas...", "Partly true", "Not true"
# ou "Fato ou Fake" não confirmam.
_TRUE_LABELS = frozenset(
    {"verdadeiro", "verdadeira", "verdade", "veridico", "correto", "correta", "fato", "true"}
    | {"comprovado", "comprovada"}
)
# Formatação invisível, controle e separadores de linha: fora do título exibido.
_INVISIBLE = {"Cf", "Cc", "Co", "Cs", "Zl", "Zp"}


def _rating_label(rating: str) -> str:
    """ "#Verídico. " -> "veridico": sem acento, minúsculo, sem pontuação nas pontas."""
    plain = unicodedata.normalize("NFKD", rating).encode("ascii", "ignore").decode()
    return " ".join(plain.lower().strip(" #.!").split())


def corroborates(rating: str) -> bool:
    """A classificação de uma agência confirma o conteúdo."""
    return _rating_label(rating) in _TRUE_LABELS


def read_verdict(veredito: str, agency_ratings: Iterable[str]) -> tuple[str, bool]:
    """Veredito a exibir e se foi rebaixado por falta de agência que confirme.

    Limitação aceita: checagens salvas antes de 11/10/2026 guardavam juntas as checagens
    citadas e as não citadas, então uma não citada que confirme mantém o "verdadeiro" antigo.
    """
    if veredito == "verdadeiro" and not any(corroborates(r) for r in agency_ratings):
        return "nao_verificavel", True
    return veredito, False


def is_open_platform(domain: str, platforms: Iterable[str]) -> bool:
    """Domínio onde qualquer pessoa publica (blog, rede social): não é "origem confirmada"."""
    host = domain.lower().strip(".")
    if is_social_host(host):
        return True
    return any(host == p or host.endswith("." + p) for p in platforms)


def _subject(title: str | None) -> str:
    """Título da página (dado do atacante): sem links nem caracteres invisíveis, e sem o
    " | G1" do fim (só sufixo curto, de até 3 palavras)."""
    text = URL_IN_TEXT.sub(" ", title or "")
    text = "".join(ch for ch in text if unicodedata.category(ch) not in _INVISIBLE)
    text = " ".join(text.split())
    head, bar, tail = text.rpartition(" | ")
    if bar and head and len(tail) <= 30 and len(tail.split()) <= 3:
        text = head
    return text.strip().rstrip(".")


SEARCH_FAILED_SENTENCE = "A busca nas agências de checagem falhou nesta análise."


def neutral_summary(
    kind: Literal["link", "texto"],
    domain: str | None,
    title: str | None,
    *,
    confirmed_origin: bool,
    agency_checks: bool = False,
    search_failed: bool = False,
) -> str:
    """Texto que substitui o resumo do modelo quando "verdadeiro" vira "Ainda sem checagem".

    confirmed_origin: o servidor baixou a página desse domínio e ele não é plataforma aberta.
    agency_checks: há checagens citadas, mas nenhuma confirma o conteúdo.
    search_failed: a busca nas agências falhou (não é "não encontramos").
    """
    if agency_checks:
        ending: str | None = "As checagens de agências encontradas não confirmam o conteúdo."
    elif search_failed:
        ending = SEARCH_FAILED_SENTENCE
    else:
        ending = None
    if kind != "link" or not domain:
        return ending or "Não encontramos checagens de agências sobre este conteúdo."
    where = f"{domain} (origem confirmada)" if confirmed_origin else domain
    subject = _subject(title)
    about = f" sobre: {subject}" if subject else ""
    ending = ending or "Não encontramos checagens de agências sobre ele."
    return f"Conteúdo publicado em {where}{about}. {ending}"


# ---------------------------------------------------------------------------------------
# Modo "só agências" (Gemini sem cota ou no limite por minuto, 12/10/2026)
# ---------------------------------------------------------------------------------------

AGENCIES_ONLY_FOUND = (
    "A análise automática está indisponível agora. Abaixo estão as checagens de agências "
    "encontradas sobre o assunto: confira se tratam do mesmo conteúdo."
)
AGENCIES_ONLY_SEARCH_FAILED = (
    "A análise automática está indisponível agora, e não conseguimos consultar as agências "
    "de checagem neste momento. Tente de novo mais tarde."
)
AGENCIES_ONLY_NONE = (
    "A análise automática está indisponível agora, e não encontramos checagens de agências "
    "sobre este conteúdo. Tente de novo mais tarde."
)


def agencies_only_result(
    checks: list[AgencyCheck],
    title: str | None,
    *,
    search_failed: bool = False,
    keys: list[str] | None = None,
) -> dict[str, Any]:
    """Resultado sem IA: só o que a Fact Check API trouxe, com texto neutro. Busca que
    falhou não é "não encontramos" (o link fica público e seria enganoso). Com `keys`, as de
    outro assunto vão para "relacionadas" (salvaguarda de relevância)."""
    found = [c.as_json() for c in checks]
    same, other = split_relevant(found, keys) if keys is not None else (found, [])
    if same:
        resumo = AGENCIES_ONLY_FOUND
    elif search_failed:
        resumo = AGENCIES_ONLY_SEARCH_FAILED
    else:
        resumo = AGENCIES_ONLY_NONE
    return {
        "modo": "agencias",
        "resumo": resumo,
        "busca_agencias": "falhou" if search_failed else "ok",
        "pontos_suspeitos": [],
        "contexto_faltante": None,
        "checagens_agencias": same,
        "checagens_relacionadas": other,
        "fontes_sugeridas": [],
        "title": title,
        **({"palavras_chave": keys} if keys is not None else {}),
    }


# ---------------------------------------------------------------------------------------
# Salvaguarda de relevância (19/10/2026): checagem de OUTRO assunto não sustenta veredito
# ---------------------------------------------------------------------------------------

# Mesmo piso do finalize (gemini.LOW_CONFIDENCE_WITHOUT_AGENCIES; um teste confere): "Falso"
# abaixo disto só com checagem de agência citada e do mesmo assunto.
FALSE_WITHOUT_AGENCY_MIN = 70


def split_relevant(
    cited: list[dict[str, Any]], keys: list[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(do mesmo assunto, de outro assunto), pelo título de cada checagem."""
    same = [c for c in cited if is_relevant(str(c.get("titulo") or ""), keys)]
    other = [c for c in cited if c not in same]
    return same, other


def relevant_verdict(
    veredito: str, confianca: int, relevant: list[dict[str, Any]]
) -> tuple[str, Literal["verdadeiro", "confianca"] | None]:
    """Veredito com a salvaguarda e o motivo do rebaixamento (None = não rebaixou)."""
    if veredito == "verdadeiro" and not any(
        corroborates(str(c.get("veredito") or "")) for c in relevant
    ):
        return "nao_verificavel", "verdadeiro"
    if veredito == "falso" and confianca < FALSE_WITHOUT_AGENCY_MIN and not relevant:
        return "nao_verificavel", "confianca"
    return veredito, None


@dataclass(frozen=True)
class Enforced:
    veredito: str
    result: dict[str, Any]
    reason: Literal["verdadeiro", "confianca"] | None


def enforce_relevance(
    *,
    veredito: str,
    confianca: int,
    result: dict[str, Any],
    keys: list[str],
    kind: Literal["link", "texto"],
    domain: str | None,
    title: str | None,
    agencies_only: bool = False,
) -> Enforced:
    """Aplica a salvaguarda ao resultado (ao gravar e na leitura; idempotente): as citadas de
    outro assunto vão para "relacionadas"; "Verdadeiro" sem citada relevante que confirme vira
    texto neutro; "Falso" abaixo de 70% sem citada relevante vira "Ainda sem checagem"."""
    cited = [c for c in result.get("checagens_agencias") or [] if isinstance(c, dict)]
    related = [c for c in result.get("checagens_relacionadas") or [] if isinstance(c, dict)]
    same, other = split_relevant(cited, keys)
    urls = {c.get("url") for c in other}
    out = {
        **result,
        "checagens_agencias": same,
        "checagens_relacionadas": other + [c for c in related if c.get("url") not in urls],
    }
    if agencies_only:
        if not same and out.get("resumo") == AGENCIES_ONLY_FOUND:
            out["resumo"] = AGENCIES_ONLY_NONE  # as encontradas são de outro assunto
        return Enforced(veredito=veredito, result=out, reason=None)
    final, reason = relevant_verdict(veredito, confianca, same)
    if reason == "verdadeiro":
        origin = out.get("origem_confirmada")
        out["resumo"] = neutral_summary(
            kind,
            origin if isinstance(origin, str) else domain,
            title,
            confirmed_origin=isinstance(origin, str),
            agency_checks=bool(same),
            search_failed=out.get("busca_agencias") == "falhou",
        )
        out["rebaixado_sem_agencia"] = True
    elif reason == "confianca":
        out["rebaixado_por_confianca"] = True
    return Enforced(veredito=final, result=out, reason=reason)
