import ipaddress

import pytest

from app.services.url_guard import (
    BlockedAddressError,
    InvalidUrlError,
    is_blocked_ip,
    is_social_media,
    normalize_url,
    pick_allowed_address,
)

# ---------- normalização ----------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://g1.globo.com/rr/noticia.html", "https://g1.globo.com/rr/noticia.html"),
        ("HTTPS://G1.Globo.COM/rr/", "https://g1.globo.com/rr/"),
        ("https://g1.globo.com", "https://g1.globo.com/"),
        ("https://g1.globo.com:443/a", "https://g1.globo.com/a"),
        ("http://g1.globo.com:80/a", "http://g1.globo.com/a"),
        ("https://g1.globo.com./a", "https://g1.globo.com/a"),
        ("https://g1.globo.com/a#comentarios", "https://g1.globo.com/a"),
        ("  https://g1.globo.com/a  ", "https://g1.globo.com/a"),
        (
            "https://site.com.br/n?id=7&utm_source=whatsapp&fbclid=XYZ&utm_medium=social&p=2",
            "https://site.com.br/n?id=7&p=2",
        ),
        ("https://site.com.br/n?gclid=1&igshid=2", "https://site.com.br/n"),
        # Parâmetros curtos e ambíguos (si, ref_src) ficam: podem ser parte da página.
        ("https://site.com.br/n?si=3", "https://site.com.br/n?si=3"),
        ("https://notícias.com.br/a", "https://xn--notcias-9ya.com.br/a"),
    ],
)
def test_normalize(raw: str, expected: str) -> None:
    assert normalize_url(raw).url == expected


def test_same_page_same_hash() -> None:
    a = normalize_url("https://Site.com.br:443/n?id=7&utm_source=zap#topo")
    b = normalize_url("https://site.com.br/n?id=7")
    assert a.url_hash == b.url_hash
    assert len(a.url_hash) == 32


def test_domain_drops_www() -> None:
    assert normalize_url("https://www.folhabv.com.br/x").domain == "folhabv.com.br"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "g1.globo.com/noticia",
        "javascript:alert(1)",
        "file:///etc/passwd",
        "ftp://site.com/a",
        "data:text/html,<script>alert(1)</script>",
        "gopher://site.com/",
        "https://",
        "https://usuario:senha@site.com/",  # pragma: allowlist secret
        "https://usuario@site.com/",
        "https://site.com:8080/",
        "https://site.com:22/",
        "https://exemplo.com/" + "a" * 2100,
        "https://site com.br/",
    ],
)
def test_invalid_urls(raw: str) -> None:
    with pytest.raises(InvalidUrlError):
        normalize_url(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "http://localhost/",
        "http://LOCALHOST:80/",
        "http://localhost./",
        "http://painel.localhost/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://servidor.internal/",
        "http://impressora.local/",
        "http://127.0.0.1/",
        "http://10.0.0.5/",
        "http://172.16.3.4/",
        "http://192.168.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://100.64.0.1/",
        "http://0.0.0.0/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://[fc00::1]/",
        "http://[fe80::1]/",
        # Formas exóticas de IPv4 (aceitas por navegadores e por inet_aton).
        "http://2130706433/",
        "http://0x7f000001/",
        "http://0177.0.0.1/",
        "http://0x7f.1/",
        "http://127.1/",
        "http://017700000001/",
        # Mesmo um IP público em forma exótica é recusado (só aceitamos a forma canônica).
        "http://134744072/",
    ],
)
def test_blocked_hosts(raw: str) -> None:
    with pytest.raises(BlockedAddressError):
        normalize_url(raw)


def test_public_ip_literal_is_allowed() -> None:
    assert normalize_url("http://8.8.8.8/x").url == "http://8.8.8.8/x"


# ---------- validação de IP (usada no momento da conexão) ----------


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "10.1.2.3",
        "172.31.255.255",
        "192.168.1.1",
        "169.254.169.254",
        "100.64.0.1",
        "100.127.255.254",
        "0.0.0.0",  # noqa: S104 — é o endereço testado, não um bind
        "0.1.2.3",
        "224.0.0.1",
        "239.255.255.250",
        "240.0.0.1",
        "255.255.255.255",
        "198.18.0.1",
        "::",
        "::1",
        "fc00::1",
        "fd12:3456::1",
        "fe80::1",
        "ff02::1",
        "::ffff:127.0.0.1",
        "::ffff:10.0.0.1",
        "64:ff9b::7f00:1",  # NAT64 -> 127.0.0.1
        "2002:7f00:1::",  # 6to4 -> 127.0.0.1
        "2001:0:4136:e378:8000:63bf:f5ff:fffe",  # Teredo com cliente 10.0.0.1
    ],
)
def test_blocked_ips(ip: str) -> None:
    assert is_blocked_ip(ipaddress.ip_address(ip))


@pytest.mark.parametrize("ip", ["8.8.8.8", "200.160.2.3", "2001:4860:4860::8888"])
def test_public_ips(ip: str) -> None:
    assert not is_blocked_ip(ipaddress.ip_address(ip))


def test_pick_allowed_address_rejects_mixed_dns_answer() -> None:
    # Rebinding: um registro público e um interno. Basta um interno para recusar.
    with pytest.raises(BlockedAddressError):
        pick_allowed_address("site.com", ["93.184.216.34", "127.0.0.1"])


def test_pick_allowed_address_returns_public_addresses() -> None:
    assert pick_allowed_address("site.com", ["93.184.216.34", "2606:2800:220:1::1"]) == [
        "93.184.216.34",
        "2606:2800:220:1::1",
    ]


def test_pick_allowed_address_without_answers() -> None:
    with pytest.raises(BlockedAddressError):
        pick_allowed_address("site.com", [])


# ---------- redes sociais ----------


@pytest.mark.parametrize(
    "raw",
    [
        "https://www.instagram.com/p/abc/",
        "https://x.com/fulano/status/1",
        "https://twitter.com/fulano/status/1",
        "https://vm.tiktok.com/abc/",
        "https://www.kwai.com/@x/video/1",
        "https://m.facebook.com/story.php?id=1",
        "https://fb.watch/abc/",
        "https://wa.me/5595999999999",
        "https://chat.whatsapp.com/abc",
        "https://t.me/canal/1",
        "https://youtu.be/abc",
    ],
)
def test_social_media(raw: str) -> None:
    assert is_social_media(normalize_url(raw))


@pytest.mark.parametrize(
    "raw",
    ["https://g1.globo.com/rr/", "https://nottwitter.com/a", "https://instagram.com.br.golpe.net/"],
)
def test_not_social_media(raw: str) -> None:
    assert not is_social_media(normalize_url(raw))


@pytest.mark.parametrize(
    "raw",
    [
        "https://www.tse.jus.br/\ufeffhttps://evil.com",
        "https://www.tse.jus.br/a\u200bb",
        "https://site.com.br/\u2028x",
    ],
)
def test_invisible_characters_are_invalid(raw: str) -> None:
    with pytest.raises(InvalidUrlError):
        normalize_url(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "http://127\u30020\u30020\u30021/",
        "http://\uff11\uff12\uff17.0.0.1/",
        "http://\uff11\uff10.0.0.1/",
    ],
)
def test_wide_ip_forms_are_blocked_before_any_connection(raw: str) -> None:
    with pytest.raises(BlockedAddressError):
        normalize_url(raw)
