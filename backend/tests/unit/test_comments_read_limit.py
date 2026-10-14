"""Limite de leitura de /comments por IP: 600/min (CGNAT das operadoras; PR cache-leitura)."""

from app.services.rate_limit import RateLimiters


def test_six_hundred_reads_per_minute_per_ip() -> None:
    limiter = RateLimiters().comments_read

    assert all(limiter.hit("1.2.3.4") is None for _ in range(600))
    assert limiter.hit("1.2.3.4") is not None  # a 601ª espera
    assert limiter.hit("5.6.7.8") is None  # outro IP não é afetado
