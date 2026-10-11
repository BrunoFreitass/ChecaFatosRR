"""Validação e normalização de URLs enviadas por usuários (anti-SSRF).

Duas barreiras:
1. `normalize_url`: recusa esquemas, portas, credenciais e hosts proibidos ANTES de qualquer
   conexão, e gera a forma canônica usada como chave do cache.
2. `pick_allowed_address`: chamada pelo `GuardedNetworkBackend` (fetcher) na hora de abrir o
   socket, com os IPs que o DNS devolveu. É o que barra DNS rebinding e redirects para a
   rede interna: a validação vale para todo socket aberto, inclusive em cada redirect.
"""

import hashlib
import ipaddress
import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import idna

MAX_URL_LENGTH = 2048
# Fontes citadas num texto: mais que isso não ajuda o Gemini e só custa CPU.
MAX_COLLECTED_URLS = 50

_ALLOWED_PORTS = {"http": 80, "https": 443}

_BLOCKED_HOSTS = {"localhost", "metadata.google.internal", "metadata"}
_BLOCKED_SUFFIXES = (".localhost", ".internal", ".local", ".home.arpa", ".lan")

_TRACKING_PREFIXES = ("utm_",)
_TRACKING_PARAMS = {
    "fbclid", "gclid", "dclid", "gbraid", "wbraid", "msclkid", "yclid", "igshid", "igsh",
    "mc_cid", "mc_eid", "_ga", "_gl",
}  # fmt: skip

# Domínios que não entregam o conteúdo sem login/JavaScript: vão direto para "cole o texto".
_SOCIAL_DOMAINS = (
    "instagram.com", "x.com", "twitter.com", "tiktok.com", "kwai.com", "facebook.com",
    "fb.watch", "fb.com", "wa.me", "whatsapp.com", "t.me", "telegram.me", "threads.net",
    "youtube.com", "youtu.be",
)  # fmt: skip

URL_IN_TEXT = re.compile(r"https?://[A-Za-z0-9\-._~:/?#@!$&*+,;=%]+", re.IGNORECASE)
URL_TRAILING = ".,;:!?)]}»"

# Partes numéricas de IPv4 nas formas que navegadores e inet_aton aceitam.
_IPV4_PART = re.compile(r"^(0x[0-9a-f]*|[0-9]+)$")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


class InvalidUrlError(ValueError):
    """Não é um link http(s) válido."""


class BlockedAddressError(ValueError):
    """Aponta para a rede interna/local (ou para um endereço que não aceitamos)."""


@dataclass(frozen=True)
class NormalizedUrl:
    url: str
    host: str
    domain: str
    url_hash: bytes


def normalize_url(raw: str) -> NormalizedUrl:
    value = raw.strip()
    if not value or len(value) > MAX_URL_LENGTH:
        raise InvalidUrlError("tamanho")
    # Espaços, controle e formatação invisível (Cf, ex.: U+FEFF, U+200B) nunca fazem parte de
    # uma URL legítima e servem para "esconder" outra URL.
    if any(unicodedata.category(ch)[0] in "CZ" for ch in value):
        raise InvalidUrlError("espaço, controle ou caractere invisível")

    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as exc:
        raise InvalidUrlError("url malformada") from exc

    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_PORTS:
        raise InvalidUrlError("esquema")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise InvalidUrlError("credenciais na url")
    if port is not None and port != _ALLOWED_PORTS[scheme]:
        raise InvalidUrlError("porta")

    host = _normalize_host(parts.hostname or "")
    netloc = f"[{host}]" if ":" in host else host
    path = parts.path or "/"
    query = _strip_tracking(parts.query)
    url = urlunsplit((scheme, netloc, path, query, ""))
    if len(url) > MAX_URL_LENGTH:
        raise InvalidUrlError("tamanho")

    domain = host.removeprefix("www.")
    return NormalizedUrl(
        url=url,
        host=host,
        domain=domain,
        url_hash=hashlib.sha256(url.encode("utf-8")).digest(),
    )


def _normalize_host(hostname: str) -> str:
    host = hostname.strip().rstrip(".").lower()
    if not host:
        raise InvalidUrlError("sem host")

    literal = _parse_ip_literal(host)
    if literal is not None:
        if is_blocked_ip(literal):
            raise BlockedAddressError("ip interno")
        return literal.compressed

    try:
        ascii_host = idna.encode(host, uts46=True).decode("ascii")
    except idna.IDNAError as exc:
        raise InvalidUrlError("domínio inválido") from exc

    # O IDNA converte pontos e dígitos "largos" (ideográficos, fullwidth): confere de novo.
    literal = _parse_ip_literal(ascii_host)
    if literal is not None:
        if is_blocked_ip(literal):
            raise BlockedAddressError("ip interno")
        return literal.compressed

    if ascii_host in _BLOCKED_HOSTS or ascii_host.endswith(_BLOCKED_SUFFIXES):
        raise BlockedAddressError("host interno")
    if "." not in ascii_host:
        # Nome sem domínio ("intranet") só resolve na rede local.
        raise BlockedAddressError("host sem domínio")
    return ascii_host


def _parse_ip_literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """IP escrito no host. Formas exóticas de IPv4 (decimal, octal, hex, abreviada) são
    recusadas mesmo quando públicas: só a forma canônica a.b.c.d é aceita."""
    if ":" in host:
        try:
            return ipaddress.IPv6Address(host.strip("[]").split("%")[0])
        except ValueError as exc:
            raise InvalidUrlError("ipv6 inválido") from exc

    labels = host.split(".")
    if not all(_IPV4_PART.match(label) for label in labels):
        return None  # é um nome de domínio
    try:
        canonical = ipaddress.IPv4Address(host)
    except ValueError:
        canonical = None
    if canonical is not None and canonical.compressed == host:
        return canonical
    raise BlockedAddressError("ip em forma não canônica")


def _strip_tracking(query: str) -> str:
    if not query:
        return ""
    kept = []
    for segment in query.split("&"):
        if not segment:
            continue
        key = segment.split("=", 1)[0].lower()
        if key in _TRACKING_PARAMS or key.startswith(_TRACKING_PREFIXES):
            continue
        kept.append(segment)
    return "&".join(kept)


def is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = _embedded_ipv4(ip)
        if embedded is not None and is_blocked_ip(embedded):
            return True
    return (
        not ip.is_global
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_unspecified
    )


def _embedded_ipv4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip.sixtofour is not None:
        return ip.sixtofour
    if ip.teredo is not None:
        return ip.teredo[1]
    if ip in _NAT64:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def pick_allowed_address(host: str, addresses: list[str]) -> list[str]:
    """Valida TODOS os IPs que o DNS devolveu. Um único interno recusa o host inteiro."""
    if not addresses:
        raise BlockedAddressError(f"sem endereço para {host}")
    allowed: list[str] = []
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%")[0])
        if is_blocked_ip(ip):
            raise BlockedAddressError("dns aponta para rede interna")
        if ip.compressed not in allowed:
            allowed.append(ip.compressed)
    return allowed


def collect_urls(text: str) -> list[str]:
    """URLs http(s) válidas citadas num texto, já normalizadas e sem repetição. Servem só
    como fontes permitidas para o Gemini: nunca são baixadas."""
    found: list[str] = []
    seen: set[str] = set()
    for match in URL_IN_TEXT.finditer(text):
        candidate = match.group(0).rstrip(URL_TRAILING)
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            url = normalize_url(candidate).url
        except (InvalidUrlError, BlockedAddressError):
            continue
        if url not in found:
            found.append(url)
            if len(found) >= MAX_COLLECTED_URLS:
                break
    return found


def is_social_media(url: NormalizedUrl) -> bool:
    return is_social_host(url.host)


def is_social_host(host: str) -> bool:
    return any(host == d or host.endswith("." + d) for d in _SOCIAL_DOMAINS)
