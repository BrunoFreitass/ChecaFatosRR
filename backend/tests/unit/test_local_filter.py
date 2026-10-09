import pytest

from app.services.moderation.local_filter import check_text, normalize


@pytest.mark.parametrize(
    "text",
    [
        "porra",
        "PORRA",
        "p0rr4",
        "p.o.r.r.a",
        "p o r r a",
        "p-o-r-r-a",
        "porrrraaaa",
        "pórra",
        "que porra é essa",
        "caralho",
        "c4r4lh0",
        "vai tomar no cu",
        "filho da puta",
        "fdp",
        "vsf",
        "buceta",
        "b u c 3 t 4",
    ],
)
def test_blocks_profanity_and_disguised_variants(text: str) -> None:
    assert check_text(text).blocked, text


@pytest.mark.parametrize(
    "text",
    [
        "cuca",
        "computador",
        "Scunthorpe",
        "enviado",
        "pintor",
        "unicórnio",
        "disputa",
        "reputação",
        "picanha",
        "carola",
        "o curso é bom",
        "Bom dia, Roraima!",
        "Boa Vista 2026",
        "a urna é segura",
        "",
    ],
)
def test_allows_innocent_text(text: str) -> None:
    assert not check_text(text).blocked, text


@pytest.mark.parametrize(
    "text", ["puta1", "cu123", "viado99", "cuuuzao", "carallho", "merdaaa", "b0st4"]
)
def test_blocks_words_glued_to_digits_or_with_repeated_letters(text: str) -> None:
    assert check_text(text).blocked, text


@pytest.mark.parametrize("text", ["não rola", "o pica-pau", "porão", "a rua", "carro", "corno"])
def test_allows_ambiguous_words_in_comments(text: str) -> None:
    assert not check_text(text).blocked, text


@pytest.mark.parametrize("text", ["rola", "pica", "corno"])
def test_ambiguous_words_are_blocked_only_in_nicknames(text: str) -> None:
    assert check_text(text, nickname=True).blocked, text


def test_result_has_category_and_rule() -> None:
    result = check_text("que buceta")

    assert result.category == "sexual"
    assert result.rule


def test_clean_result_has_no_category() -> None:
    result = check_text("bom dia")

    assert result.category is None
    assert result.rule is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("Pórr4", "porra"), ("CARALHO", "caralho"), ("ação", "acao"), ("$3x0", "sexo")],
)
def test_normalize(raw: str, expected: str) -> None:
    assert normalize(raw) == expected
