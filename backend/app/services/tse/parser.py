"""JSON de resultado do TSE (formato de 2026, arquivo "-u.json") -> modelo interno.

Arquivo: <base>/ele2026/<eleicao>/dados/<uf|br>/<uf|br>-c<cargo>-e<eleicao>-u.json.
Números vêm como texto com vírgula decimal ("47,03"); data e hora em campos separados
(dd/mm/aaaa e hh:mm:ss, horário de Brasília). Tolerante: campo que falta ou valor estranho vira
None (não derruba a página); estrutura inesperada (sem candidatos) levanta TseFormatError e o
serviço mantém o último resultado bom.

"Eleito" só quando a SITUAÇÃO do TSE disser (Eleito/Eleita/Matematicamente eleito): no 1º
turno o TSE marca "e": "s" também para quem vai ao 2º turno.
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

BRASILIA = ZoneInfo("America/Sao_Paulo")
MAX_TEXT = 120
# Presidente tem uma dúzia de candidatos no 1º turno e 2 no 2º: muito mais é arquivo errado.
MAX_CANDIDATES = 50
_DIGITS = re.compile(r"\d+", re.ASCII)
_ELECTED = re.compile(r"^(?:matematicamente\s+)?eleit[oa]\b", re.IGNORECASE)


class TseFormatError(ValueError):
    """O arquivo do TSE não tem a estrutura esperada."""


@dataclass(frozen=True)
class Candidate:
    numero: str
    nome: str
    partido: str | None
    vice: str | None
    votos: int
    pct: float | None
    situacao: str
    eleito: bool


@dataclass(frozen=True)
class TseResult:
    escopo: str
    eleicao: str
    turno: int | None
    cargo: str
    final: bool
    totalizado_em: datetime | None
    secoes_pct: float | None
    eleitores: int | None
    validos: int | None
    brancos: int | None
    brancos_pct: float | None
    nulos: int | None
    nulos_pct: float | None
    abstencao: int | None
    abstencao_pct: float | None
    candidatos: tuple[Candidate, ...]


def parse_result(raw: Any) -> TseResult:
    if not isinstance(raw, dict):
        raise TseFormatError("raiz não é objeto")
    cargos = raw.get("carg")
    if not isinstance(cargos, list) or not cargos or not isinstance(cargos[0], dict):
        raise TseFormatError("sem cargo")
    cargo = cargos[0]
    found = list(_candidates(cargo))
    if not found:
        raise TseFormatError("sem candidatos")
    if len(found) > MAX_CANDIDATES:
        raise TseFormatError("candidatos demais")
    section = _dict(raw.get("s"))
    votes = _dict(raw.get("v"))
    voters = _dict(raw.get("e"))
    return TseResult(
        escopo=_text(raw.get("cdabr")) or "",
        eleicao=_text(raw.get("ele")) or "",
        turno=_int(raw.get("t")),
        cargo=_text(cargo.get("nmn")) or "",
        final=raw.get("tf") == "s",
        totalizado_em=_when(raw.get("dt"), raw.get("ht")),
        secoes_pct=_pct(section.get("pst")),
        eleitores=_int(voters.get("te")),
        validos=_int(votes.get("vv")),
        brancos=_int(votes.get("vb")),
        brancos_pct=_pct(votes.get("pvb")),
        nulos=_int(votes.get("tvn")),
        nulos_pct=_pct(votes.get("ptvn")),
        abstencao=_int(voters.get("a")),
        abstencao_pct=_pct(voters.get("pa")),
        candidatos=tuple(_ordered(found)),
    )


def _candidates(cargo: dict[str, Any]) -> Iterator[tuple[dict[str, Any], str | None]]:
    groups = cargo.get("agr")
    if not isinstance(groups, list):
        raise TseFormatError("agr não é lista")
    for group in groups:
        for party in _list(_dict(group).get("par")):
            party_data = _dict(party)
            for cand in _list(party_data.get("cand")):
                if isinstance(cand, dict) and _text(cand.get("n")):
                    yield cand, _text(party_data.get("sg"))


def _ordered(found: list[tuple[dict[str, Any], str | None]]) -> list[Candidate]:
    """Ordem do TSE (seq); sem ela, por votos."""

    def key(item: tuple[dict[str, Any], str | None]) -> tuple[int, int]:
        cand = item[0]
        seq = _int(cand.get("seq"))
        return (seq if seq is not None else 10_000, -(_int(cand.get("vap")) or 0))

    out = []
    for cand, party in sorted(found, key=key):
        situacao = _text(cand.get("st")) or ""
        vices = [_text(_dict(v).get("nmu")) for v in _list(cand.get("vs"))]
        out.append(
            Candidate(
                numero=_text(cand.get("n")) or "",
                nome=_text(cand.get("nmu")) or _text(cand.get("nm")) or "",
                partido=party,
                vice=next((v for v in vices if v), None),
                votos=_int(cand.get("vap")) or 0,
                pct=_pct(cand.get("pvap")),
                situacao=situacao,
                eleito=bool(_ELECTED.match(situacao)),
            )
        )
    return out


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _text(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, str | int):
        return None
    text = " ".join(str(value).split())
    return text[:MAX_TEXT] or None


def _int(value: Any) -> int | None:
    text = _text(value)
    if text is None:
        return None
    digits = text.replace(".", "")  # "56.104.503"
    return int(digits) if _DIGITS.fullmatch(digits) else None  # só 0-9 (não "²")


def _pct(value: Any) -> float | None:
    """Percentual do TSE ("47,03"; aceita "47.03"); fora de 0-100 não é confiável."""
    text = _text(value)
    if text is None:
        return None
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    try:
        number = float(text)
    except ValueError:
        return None
    return number if 0 <= number <= 100 else None


def _when(day: Any, hour: Any) -> datetime | None:
    d, h = _text(day), _text(hour)
    if not d or not h:
        return None
    try:
        when = datetime.strptime(f"{d} {h}", "%d/%m/%Y %H:%M:%S").replace(tzinfo=BRASILIA)
    except ValueError:
        return None
    # Data no futuro travaria o "só grava o mais novo" do snapshot: descarta.
    return when if when <= datetime.now(UTC) + timedelta(days=1) else None
