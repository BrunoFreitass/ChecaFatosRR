"""Salvaguarda de relevância (19/10/2026): checagem de OUTRO assunto não sustenta "Verdadeiro"
nem "Falso" abaixo de 70%. Casos reais do diagnóstico de 19/10 (títulos das checagens)."""

from app.services.relevance import MIN_SHARED, content_keys, is_relevant, shared_keys

WHATSAPP = "No domingo agora só quem vota no Flávio e no outro domingo é no ladrão. Repassem!"
CASO1 = (
    "O TSE decidiu que só eleitores de Flávio Bolsonaro votam no dia 4 de outubro; os eleitores "
    "de Lula votam no domingo seguinte."
)
CASO3 = (
    "Atenção: quem não votou no primeiro turno não pode votar no segundo turno. "
    "A Justiça Eleitoral vai barrar na seção quem faltou no dia 4."
)
CASO4 = "Lula gastou R$ 7 bilhões em viagens pessoais com dinheiro público durante o governo."
CASO5 = (
    "Na entrevista à TV Globo, Flávio Bolsonaro disse que não tem nenhuma relação com o Banco "
    "Master nem com Daniel Vorcaro."
)
AOS_FATOS_DIA_4 = "É falso que só eleitores de Flávio poderão votar no dia 4 de outubro"


def test_keys_ignore_accents_case_and_empty_words() -> None:
    assert content_keys("É FALSO que a Votação foi fraudada") == ["votacao", "fraudada"]


def test_candidate_names_and_tse_do_not_count_as_shared_subject() -> None:
    # Aparecem em quase toda alegação desta eleição: sozinhos não indicam o mesmo assunto.
    assert content_keys("Lula, Flávio Bolsonaro, Janja e o TSE") == []
    assert content_keys("STF, PT, PL, Moraes, eleição, urna e presidente") == []


def test_shared_keys_match_by_prefix_and_numbers_only_exactly() -> None:
    assert shared_keys(content_keys("votou no dia"), content_keys("votou dias")) == 2
    # Palavra curta só igual (prefixo curto casaria palavras sem relação: pix/pixel).
    assert shared_keys(content_keys("pix hoje"), content_keys("pixel hoje")) == 1
    assert shared_keys(content_keys("dia 4"), content_keys("dia 45")) == 1  # 4 ≠ 45
    assert MIN_SHARED == 2


def test_same_subject_checks_are_relevant() -> None:
    keys = content_keys(CASO4)
    assert is_relevant("Lula não gastou R$ 7 bilhões em viagens pessoais", keys)
    assert is_relevant("É falso que Lula e Janja gastaram R$ 7,35 bilhões em viagens", keys)
    assert is_relevant("Viagens que custaram R$ 7,35 bilhões são de todo o Poder ...", keys)
    assert is_relevant("Quem não votou no primeiro turno não pode vota", content_keys(CASO3))
    assert is_relevant(AOS_FATOS_DIA_4, content_keys(CASO1))


def test_other_subject_checks_are_not_relevant_case_5() -> None:
    keys = content_keys(CASO5)
    # Só "Master" em comum ("relação" e "fala" são genéricas): outro assunto.
    assert not is_relevant(
        "Fala de Lula sobre delações não é atual nem tem relação com Master", keys
    )
    # Só os nomes do candidato em comum: outro assunto.
    assert not is_relevant("Checamos a live de Flávio Bolsonaro", keys)
    assert not is_relevant(
        "Vídeo em que Flávio anuncia cortes em pensões, 13º e Pé-de-Meia ...", keys
    )


def test_unrelated_election_check_is_not_relevant() -> None:
    keys = content_keys(CASO4)
    assert not is_relevant("Pausa na divulgação de resultados não comprova fraude eleitoral", keys)


def test_very_different_wording_stays_conservative_whatsapp() -> None:
    # A mensagem e a checagem dizem a mesma coisa com palavras diferentes ("vota" é a única em
    # comum): a checagem não sustenta veredito sozinha e fica em "relacionadas".
    assert not is_relevant(AOS_FATOS_DIA_4, content_keys(WHATSAPP))


def test_simple_plural_and_the_election_year_and_round() -> None:
    assert shared_keys(content_keys("votos anulados"), content_keys("voto anulado")) == 2
    assert content_keys("Em 2026, no segundo turno") == []


def test_empty_inputs_are_never_relevant() -> None:
    assert not is_relevant("", content_keys(CASO4))
    assert not is_relevant("Lula gastou bilhões em viagens", [])
