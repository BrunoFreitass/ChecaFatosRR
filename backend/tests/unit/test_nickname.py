import pytest

from app.services.nicknames import validate_nickname


@pytest.mark.parametrize(
    "apelido",
    [
        "bruno_rr",
        "Joao1990",
        "maria_bv",
        "abc",
        "a" * 20,
        "Picanha_BV",
        "ana2026",
        "Boa_Vista",
        "badminton_rr",
        "Carolina",
    ],
)
def test_accepts_valid_nicknames(apelido: str) -> None:
    verdict = validate_nickname(apelido)

    assert verdict.ok, verdict


@pytest.mark.parametrize(
    "apelido",
    ["ab", "a" * 21, "joão", "nome com espaço", "x-y", "", "maria.bv", "<script>"],
)
def test_rejects_bad_format(apelido: str) -> None:
    verdict = validate_nickname(apelido)

    assert not verdict.ok
    assert verdict.reason == "formato"


@pytest.mark.parametrize(
    "apelido",
    [
        "admin",
        "Admin_RR",
        "AdminRR",
        "adm1n",
        "moderador",
        "m0derador",
        "ChecaFatosRR",
        "checaf4tos",
        "tse",
        "suporte_oficial",
        "TSE_RR",
    ],
)
def test_rejects_reserved(apelido: str) -> None:
    verdict = validate_nickname(apelido)

    assert not verdict.ok
    assert verdict.reason == "reservado"


@pytest.mark.parametrize(
    "apelido",
    ["p0rr4", "caralho_rr", "xXbucetaXx", "fdp123", "PoRrA", "puta1", "cu_99", "rola_rr"],
)
def test_rejects_forbidden_content(apelido: str) -> None:
    verdict = validate_nickname(apelido)

    assert not verdict.ok
    assert verdict.reason == "conteudo_proibido"
    assert verdict.filter_result is not None
    assert verdict.filter_result.category
