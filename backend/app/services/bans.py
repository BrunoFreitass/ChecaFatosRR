"""Identificação de contas banidas sem guardar o identificador do Google em claro."""

import hashlib
import hmac


def ban_hash(google_sub: str, *, salt: str) -> bytes:
    """HMAC-SHA256(BAN_HASH_SALT, sub da identidade Google) — 32 bytes.

    Usa o `sub` do Google (auth.identities.provider_id), e não o id do usuário no Supabase:
    se a pessoa apagar a conta e entrar de novo com o mesmo Google, o id do Supabase muda,
    mas o `sub` do Google continua o mesmo.
    """
    if not google_sub or not salt:
        raise ValueError("google_sub e salt são obrigatórios")
    return hmac.new(salt.encode(), google_sub.encode(), hashlib.sha256).digest()
