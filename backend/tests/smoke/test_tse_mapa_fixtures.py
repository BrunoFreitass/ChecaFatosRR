"""Os 27 arquivos oficiais por UF do 1º turno (6257), baixados uma vez em 14/10/2026 com
autorização do usuário: o parser e o resumo do mapa funcionam com o formato real do TSE."""

import json
from pathlib import Path

import pytest

from app.services.tse.mapa import UFS, summarize
from app.services.tse.parser import parse_result

FIXTURES = Path(__file__).parents[1] / "fixtures" / "tse" / "uf"


@pytest.mark.parametrize("uf", UFS)
def test_official_state_file_parses_into_a_map_summary(uf: str) -> None:
    raw = json.loads((FIXTURES / f"{uf}-c0001-e006257-u.json").read_text(encoding="utf-8"))

    result = parse_result(raw)
    resumo = summarize(result)

    assert result.eleicao == "6257"
    assert str(raw["cdabr"]).lower() == uf  # o arquivo é mesmo da UF pedida
    names = {c["numero"]: c["nome"] for c in resumo["candidatos"]}
    # Cores do mapa (TSE_MAPA_CORES) confirmadas pelo usuário com estes números.
    assert names["13"] == "LULA" and names["22"] == "FLAVIO BOLSONARO"
    assert resumo["secoes_pct"] == 100.0
    assert "eleit" not in json.dumps(resumo).lower()
