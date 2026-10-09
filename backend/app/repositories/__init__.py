"""Acesso ao banco com SQL explícito.

Convenção: cada método público é UMA transação (`async with session.begin()`), para que
regras de concorrência (on conflict, for update) fiquem visíveis em um só lugar.
"""
