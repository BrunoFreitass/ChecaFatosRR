import contextlib
import time

import pytest

from app.services.text_input import (
    MAX_TEXT_CHARS,
    TextTooLongError,
    TextTooShortError,
    prepare_text,
)

MSG = "URGENTE: o TSE anunciou que a votação do 2º turno em Roraima foi adiada para 1º de novembro!"


def test_keeps_message() -> None:
    assert prepare_text(MSG).body == MSG


def test_removes_invisible_characters_and_forward_markers() -> None:
    raw = f"Encaminhada com frequência\n\u200b{MSG}\u200d\ufeff"
    assert prepare_text(raw).body == MSG


def test_collapses_spaces_but_keeps_paragraphs() -> None:
    raw = "Primeira   linha\t com espaços\n\n\n\n\nSegunda linha com mais texto aqui"
    expected = "Primeira linha com espaços\n\nSegunda linha com mais texto aqui"
    assert prepare_text(raw).body == expected


def test_same_message_same_hash_regardless_of_case_and_spacing() -> None:
    a = prepare_text(MSG)
    b = prepare_text("  " + MSG.upper().replace(" ", "   ") + "\n\n")
    c = prepare_text("Encaminhada\n" + MSG)
    assert a.text_hash == b.text_hash == c.text_hash
    assert len(a.text_hash) == 32


def test_different_messages_different_hash() -> None:
    assert prepare_text(MSG).text_hash != prepare_text(MSG + " Compartilhe!").text_hash


def test_nfkc() -> None:
    # Letras "estilizadas" (comuns em correntes) viram letras normais.
    bold_urgente = "\U0001d414\U0001d411\U0001d406\U0001d404\U0001d40d\U0001d413\U0001d404"
    assert prepare_text(bold_urgente + " " + MSG).body.startswith("URGENTE URGENTE")


@pytest.mark.parametrize(
    ("raw", "masked"),
    [
        ("Ligue (95) 99123-4567 agora", "Ligue [telefone oculto] agora"),
        ("Ligue +55 95 991234567 agora", "Ligue [telefone oculto] agora"),
        ("Ligue 95991234567 agora", "Ligue [telefone oculto] agora"),
        ("Ligue 3623-1234 agora", "Ligue [telefone oculto] agora"),
        ("Escreva para fulano.silva@gmail.com hoje", "Escreva para [e-mail oculto] hoje"),
        ("CPF do eleitor 123.456.789-09 bloqueado", "CPF do eleitor [CPF oculto] bloqueado"),
        (
            "Pix 123e4567-e89b-12d3-a456-426614174000 para ajudar",
            "Pix [chave Pix oculta] para ajudar",
        ),
    ],
)
def test_masks_personal_data(raw: str, masked: str) -> None:
    padding = " — mensagem recebida no grupo da família"
    assert prepare_text(raw + padding).body == masked + padding


@pytest.mark.parametrize(
    "keep",
    [
        "Votos válidos: 58.206.354 no 2º turno",
        "Doação de R$ 1.500,00 ao partido",
        "Eleição em 25/10/2026 às 17h",
        "Aprovação de 47,8% segundo o instituto",
        "Seção 0123 da zona 1",
        "Lei 9.504/97, art. 33",
    ],
)
def test_does_not_mask_ordinary_numbers(keep: str) -> None:
    text = keep + " conforme a mensagem que circula"
    assert prepare_text(text).body == text


def test_collects_urls_without_downloading() -> None:
    result = prepare_text(MSG + " Veja https://www.tse.jus.br/comunicacao e http://localhost/x")
    assert result.urls == ["https://www.tse.jus.br/comunicacao"]


def test_too_short() -> None:
    with pytest.raises(TextTooShortError):
        prepare_text("isso é verdade?")
    with pytest.raises(TextTooShortError):
        prepare_text("!!! ??? ... 🙏🙏🙏🙏🙏🙏🙏🙏🙏🙏🙏🙏🙏🙏🙏🙏🙏🙏🙏🙏")


def test_too_long() -> None:
    with pytest.raises(TextTooLongError):
        prepare_text("a" * (MAX_TEXT_CHARS + 1))


def test_limit_counts_after_cleanup() -> None:
    # Espaços em excesso não contam contra o limite.
    raw = ("palavra " + " " * 10) * 500
    assert len(raw) > MAX_TEXT_CHARS
    assert len(prepare_text(raw).body) <= MAX_TEXT_CHARS


PADDING = " — mensagem que circula no grupo da família"


def test_masks_bare_cpf_only_when_check_digits_match() -> None:
    assert prepare_text("CPF 52998224725" + PADDING).body == "CPF [CPF oculto]" + PADDING
    # 11 dígitos que não são CPF válido (protocolo, título de eleitor parcial) ficam.
    assert prepare_text("Protocolo 12345678901" + PADDING).body == "Protocolo 12345678901" + PADDING


def test_masks_cnpj() -> None:
    assert prepare_text("CNPJ 11.222.333/0001-81" + PADDING).body == "CNPJ [CNPJ oculto]" + PADDING


def test_message_with_only_contact_data_is_too_short() -> None:
    with pytest.raises(TextTooShortError):
        prepare_text("Ligue (95) 99123-4567 ou fulano.silva@gmail.com agora")


@pytest.mark.parametrize("raw", ["a" * 9_000, "a." * 4_500, "9" * 9_000, "a@" * 4_500])
def test_long_runs_without_separators_are_fast(raw: str) -> None:
    # Regressão de ReDoS: o mascaramento roda no event loop.
    started = time.perf_counter()
    with contextlib.suppress(TextTooLongError, TextTooShortError):
        prepare_text(raw)
    assert time.perf_counter() - started < 0.5


@pytest.mark.parametrize(
    "keep",
    [
        "O governo desviou R$ 2500000000 do fundo eleitoral",
        "Processo número 1234567890 no tribunal",
        "Foram 58206354 votos no segundo turno",
    ],
)
def test_long_plain_numbers_are_not_phones(keep: str) -> None:
    assert prepare_text(keep + PADDING).body == keep + PADDING


def test_year_ranges_are_not_phones() -> None:
    text = "O mandato 2023-2026 terminou e o de 1999-2002 também" + PADDING
    assert prepare_text(text).body == text


def test_urls_with_personal_data_are_not_collected() -> None:
    raw = "Fale comigo no https://wa.me/5595991234567 sobre a eleição" + PADDING
    result = prepare_text(raw)
    assert "5595991234567" not in result.body
    assert all("5595991234567" not in url for url in result.urls)
