"""Montagem das consultas à Google Fact Check Tools API (diagnóstico de 19/10/2026: a frase
longa só acha quando é quase igual ao título da checagem; palavras-chave acham muito mais)."""

from app.services.factcheck import MAX_QUERIES, build_queries, keywords


def test_keywords_keep_the_order_and_drop_empty_and_boilerplate_words() -> None:
    assert keywords("É falso que só eleitores de Flávio poderão votar no dia 4 de outubro") == (
        "eleitores Flávio poderão votar dia"
    )
    assert keywords("Checamos a entrevista de Flávio Bolsonaro na TV Globo; veja o resultado") == (
        "entrevista Flávio Bolsonaro TV Globo"
    )


def test_keywords_keep_numbers_acronyms_and_names() -> None:
    assert (
        keywords("Lula gastou R$ 7 bilhões em viagens em 2025") == "Lula gastou 7 bilhões viagens"
    )


def test_keywords_without_enough_content_is_none() -> None:
    assert keywords("é falso que") is None
    assert keywords("veja") is None
    assert keywords("") is None


def test_link_queries_title_then_title_keywords_then_text_keywords() -> None:
    title = "Checamos a entrevista de Flávio Bolsonaro na TV Globo; veja o resultado"
    text = (
        "O Estadão Verifica checou as falas de Flávio Bolsonaro na entrevista à TV Globo. A "
        "declaração sobre o Banco Master e Daniel Vorcaro foi classificada como falsa."
    )
    assert build_queries(title, text) == [
        "Checamos a entrevista de Flávio Bolsonaro na TV Globo veja o resultado",
        "entrevista Flávio Bolsonaro TV Globo",
        "Estadão Verifica checou falas Flávio",
    ]


def test_pasted_text_queries_first_sentence_its_keywords_and_the_next_sentence() -> None:
    text = (
        "No domingo agora só quem vota no Flávio e no outro domingo é no ladrão. "
        "O TSE separou os eleitores por candidato, repassem!"
    )
    assert build_queries(None, text) == [
        "No domingo agora só quem vota no Flávio e no outro domingo é no ladrão",
        "domingo vota Flávio ladrão",
        "TSE separou eleitores candidato",
    ]


def test_at_most_three_queries_without_repetition() -> None:
    # Título que já é só palavra-chave: a 2ª e a 3ª seriam iguais à 1ª e saem.
    queries = build_queries("Banco Master Vorcaro Flávio", "Banco Master Vorcaro Flávio. Texto.")
    assert queries == ["Banco Master Vorcaro Flávio"]
    long_text = " ".join(
        f"Frase {i} sobre urnas eletrônicas fraude TSE eleição." for i in range(20)
    )
    many = build_queries("Título sobre urnas e fraude na eleição", long_text)
    assert len(many) <= MAX_QUERIES
    assert len(many) == len({q.casefold() for q in many})


def test_urls_never_go_into_a_query() -> None:
    queries = build_queries(
        None, "Veja https://exemplo.com/fraude-nas-urnas agora: fraude nas urnas do TSE confirmada"
    )
    assert queries
    assert all("http" not in q and "exemplo" not in q for q in queries)


def test_too_little_text_has_no_query() -> None:
    assert build_queries(None, "oi") == []
