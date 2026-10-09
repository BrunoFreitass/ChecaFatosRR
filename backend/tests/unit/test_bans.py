import pytest

from app.services.bans import ban_hash


def test_is_deterministic_and_32_bytes() -> None:
    first = ban_hash("109876543210987654321", salt="sal")

    assert first == ban_hash("109876543210987654321", salt="sal")
    assert len(first) == 32


def test_known_answer_guards_against_silent_algorithm_change() -> None:
    # HMAC-SHA256(key="sal", msg="google-sub-1"). Se este valor mudar, todos os banimentos
    # já gravados deixam de valer — não altere o algoritmo sem migrar banned_identities.
    expected = "0845c2b0674572d61d13983ba3e68508f488edc95dbde6d00f617a1fc55acad4"

    assert ban_hash("google-sub-1", salt="sal").hex() == expected


def test_depends_on_salt_and_sub() -> None:
    base = ban_hash("google-sub-1", salt="sal-a")

    assert base != ban_hash("google-sub-1", salt="sal-b")
    assert base != ban_hash("google-sub-2", salt="sal-a")


def test_does_not_contain_the_sub_in_clear() -> None:
    assert b"google-sub-1" not in ban_hash("google-sub-1", salt="sal")


@pytest.mark.parametrize(("sub", "salt"), [("", "sal"), ("google-sub", "")])
def test_rejects_empty_inputs(sub: str, salt: str) -> None:
    with pytest.raises(ValueError):
        ban_hash(sub, salt=salt)
