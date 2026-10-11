"""Regras de veredito que valem na análise (finalize) E na leitura (página pública, Últimas).

Política de 10-11/10/2026: origem confirmada não é fato verificado. "Verdadeiro" exige uma
checagem de agência cuja classificação confirme o conteúdo; sem ela, "nao_verificavel"
("Ainda sem checagem") com um texto neutro no lugar do resumo do modelo. Aplicar também na
leitura corrige checagens salvas antes da regra, sem mexer no banco.
"""

import unicodedata
from collections.abc import Iterable
from typing import Literal

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


def neutral_summary(
    kind: Literal["link", "texto"],
    domain: str | None,
    title: str | None,
    *,
    confirmed_origin: bool,
    agency_checks: bool = False,
) -> str:
    """Texto que substitui o resumo do modelo quando "verdadeiro" vira "Ainda sem checagem".

    confirmed_origin: o servidor baixou a página desse domínio e ele não é plataforma aberta.
    agency_checks: há checagens citadas, mas nenhuma confirma o conteúdo.
    """
    ending = (
        "As checagens de agências encontradas não confirmam o conteúdo." if agency_checks else None
    )
    if kind != "link" or not domain:
        return ending or "Não encontramos checagens de agências sobre este conteúdo."
    where = f"{domain} (origem confirmada)" if confirmed_origin else domain
    subject = _subject(title)
    about = f" sobre: {subject}" if subject else ""
    ending = ending or "Não encontramos checagens de agências sobre ele."
    return f"Conteúdo publicado em {where}{about}. {ending}"
