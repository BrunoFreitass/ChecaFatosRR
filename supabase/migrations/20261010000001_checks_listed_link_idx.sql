-- "Últimas" mostra só a checagem listada mais recente de cada link (anti-join "not exists").
-- Este índice parcial atende a subconsulta por link; checks_listed_idx atende a ordem da lista.
-- Sem CONCURRENTLY: migração roda em transação, e a tabela ainda é pequena (antes do lançamento).
create index if not exists checks_listed_link_idx
  on public.checks (link_id, created_at desc, public_id desc)
  where listed and link_id is not null;
