"""Parser do resultado do TSE (etapa 5) — TESTE OBRIGATÓRIO.

Fixture real: 1º turno de 2026 (eleição 6257), presidente, Brasil, baixada em 11/10/2026 de
resultados.tse.jus.br/oficial/ele2026/6257/dados/br/br-c0001-e006257-u.json. Os casos do 2º
turno (parcial, final, campos faltando) são variações dela: o formato de 2026 é o mesmo para
o 2º turno (eleição 6258).
"""

import copy
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app.services.tse.parser import TseFormatError, parse_result

FIXTURE = Path(__file__).parents[1] / "fixtures" / "tse" / "br-c0001-e006257-u.json"
BRASILIA = ZoneInfo("America/Sao_Paulo")


def raw() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def candidates(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for agr in data["carg"][0]["agr"] for par in agr["par"] for c in par["cand"]]


def second_round(pst: str, st_flavio: str = "", st_lula: str = "") -> dict[str, Any]:
    """2º turno simulado: só os dois classificados, votos e situação dados."""
    data = raw()
    data["ele"], data["t"] = "6258", "2"
    data["s"]["pst"] = pst
    agrs = []
    for agr in data["carg"][0]["agr"]:
        for par in agr["par"]:
            par["cand"] = [c for c in par["cand"] if c["n"] in ("22", "13")]
        agr["par"] = [p for p in agr["par"] if p["cand"]]
        if agr["par"]:
            agrs.append(agr)
    data["carg"][0]["agr"] = agrs
    for c in candidates(data):
        flavio = c["n"] == "22"
        c["e"] = "s" if (st_flavio if flavio else st_lula).lower().startswith("eleit") else "n"
        c["st"] = st_flavio if flavio else st_lula
        c["vap"] = "1000" if flavio else "900"
        c["pvap"] = "52,63" if flavio else "47,37"
        c["seq"] = "1" if flavio else "2"
    return data


# ---------- arquivo real ----------


def test_real_first_round_file() -> None:
    result = parse_result(raw())

    assert result.escopo == "br"
    assert result.eleicao == "6257"
    assert result.turno == 1
    assert result.cargo == "Presidente"
    assert result.secoes_pct == 100.0
    assert result.final is True
    assert result.totalizado_em == datetime(2026, 10, 5, 12, 51, 5, tzinfo=BRASILIA)
    assert result.eleitores == 158_745_502
    assert result.validos == 119_300_788
    assert (result.brancos, result.brancos_pct) == (2_300_798, 1.84)
    assert (result.nulos, result.nulos_pct) == (3_674_249, 2.93)
    assert (result.abstencao, result.abstencao_pct) == (33_469_244, 21.08)
    assert len(result.candidatos) == 12


def test_candidates_follow_the_tse_order_with_votes_and_party() -> None:
    first, second = parse_result(raw()).candidatos[:2]

    assert (first.numero, first.nome, first.partido) == ("22", "FLAVIO BOLSONARO", "PL")
    assert (first.votos, first.pct) == (56_104_503, 47.03)
    assert first.vice == "ALFREDO GASPAR"
    assert (second.numero, second.nome, second.votos) == ("13", "LULA", 53_879_538)


def test_going_to_the_second_round_is_not_elected() -> None:
    # No 1º turno o TSE marca "e": "s" para os classificados ("st": "2º turno"): NÃO é eleito.
    result = parse_result(raw())
    assert not any(c.eleito for c in result.candidatos)
    assert result.candidatos[0].situacao == "2º turno"


# ---------- 2º turno (variações do arquivo real) ----------


def test_partial_second_round_has_no_elected() -> None:
    result = parse_result(second_round("45,30"))

    assert result.turno == 2
    assert result.secoes_pct == 45.3
    assert [c.numero for c in result.candidatos] == ["22", "13"]
    assert not any(c.eleito for c in result.candidatos)


@pytest.mark.parametrize("status", ["Eleito", "Eleita", "Matematicamente eleito"])
def test_elected_only_when_the_tse_says_so(status: str) -> None:
    result = parse_result(second_round("99,10", st_flavio=status, st_lula="Não eleito"))

    flavio, lula = result.candidatos
    assert flavio.eleito is True and flavio.situacao == status
    assert lula.eleito is False


def test_zero_sections_before_the_count_starts() -> None:
    data = second_round("0,00")
    for c in candidates(data):
        c["vap"], c["pvap"] = "0", "0,00"
    result = parse_result(data)
    assert result.secoes_pct == 0.0
    assert all(c.votos == 0 for c in result.candidatos)


# ---------- tolerância ----------


def test_missing_optional_fields_become_none() -> None:
    data = raw()
    del data["dt"], data["ht"], data["v"]["vb"], data["e"]["a"]
    for c in candidates(data):
        c.pop("vs", None)
    result = parse_result(data)
    assert result.totalizado_em is None
    assert result.brancos is None and result.abstencao is None
    assert result.candidatos[0].vice is None


def test_numbers_with_thousand_dots_and_bad_values() -> None:
    data = raw()
    first = candidates(data)[0]
    first["vap"], first["pvap"] = "56.104.503", "x"
    data["s"]["pst"] = ""
    result = parse_result(data)
    flavio = next(c for c in result.candidatos if c.numero == "22")
    assert flavio.votos == 56_104_503
    assert flavio.pct is None
    assert result.secoes_pct is None


def test_candidates_without_order_fall_back_to_votes() -> None:
    data = second_round("10,00")
    for c in candidates(data):
        del c["seq"]
    assert [c.numero for c in parse_result(data).candidatos] == ["22", "13"]


@pytest.mark.parametrize(
    "broken",
    [
        [],
        "texto",
        {},
        {"carg": []},
        {"carg": [{"agr": "x"}]},
        {"carg": [{"cd": "1", "agr": []}]},
    ],
)
def test_unexpected_structure_raises_format_error(broken: Any) -> None:
    with pytest.raises(TseFormatError):
        parse_result(broken)


def test_text_fields_are_trimmed_and_bounded() -> None:
    data = raw()
    first = candidates(data)[0]
    first["nmu"] = "  " + "A" * 500 + "  "
    name = next(c for c in parse_result(data).candidatos if c.numero == "22").nome
    assert len(name) == 120 and not name.startswith(" ")


def test_parse_does_not_mutate_the_input() -> None:
    data = raw()
    before = copy.deepcopy(data)
    parse_result(data)
    assert data == before


# ---------- revisão (11/10/2026) ----------


def test_unicode_digits_do_not_crash_the_parser() -> None:
    data = raw()
    candidates(data)[0]["vap"] = "²"
    flavio = next(c for c in parse_result(data).candidatos if c.numero == "22")
    assert flavio.votos == 0


def test_percent_with_a_decimal_point_and_out_of_range() -> None:
    data = raw()
    first = candidates(data)[0]
    first["pvap"] = "47.03"
    data["s"]["pst"] = "4703"
    result = parse_result(data)
    assert next(c for c in result.candidatos if c.numero == "22").pct == 47.03
    assert result.secoes_pct is None  # percentual fora de 0-100 não é confiável


def test_too_many_candidates_is_a_format_error() -> None:
    data = raw()
    par = data["carg"][0]["agr"][0]["par"][0]
    par["cand"] = [{"n": str(i), "nmu": "X", "vap": "1"} for i in range(60)]
    with pytest.raises(TseFormatError):
        parse_result(data)


def test_totalization_in_the_future_is_ignored() -> None:
    data = raw()
    data["dt"] = "01/01/2099"
    assert parse_result(data).totalizado_em is None
