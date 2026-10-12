-- ChecaFatosRR — etapas 3+4: comentários + moderação.
-- Aplicar depois de 20261010000001_checks_listed_link_idx.sql (SQL Editor ou `supabase db push`).
-- As tabelas de comentários e moderação já existem desde o init (ainda vazias em produção);
-- aqui entram o id público, a remoção pelo autor e a suspensão que sobrevive à exclusão.
-- Etapa 9: sem tabela nova (as policies do app_backend continuam valendo).

-- ===== Comentários: id público aleatório e remoção pelo autor =====
-- public_id: 12 caracteres base32 (Crockford, minúsculo), gerado no backend, como em checks.
-- O bigint é só interno (pai/filho, FKs) e nunca aparece em URL, resposta ou cursor.
-- "not null" direto: nenhum comentário foi gravado até esta etapa.
-- removed_by_author: o texto é apagado (body null) e o comentário vira "Comentário removido
-- pelo autor"; as respostas continuam.
alter table public.comments
  add column public_id text not null,
  add constraint comments_public_id_key unique (public_id),
  add constraint comments_public_id_format check (public_id ~ '^[0-9a-hjkmnp-tv-z]{12}$'),
  alter column body drop not null,
  drop constraint comments_status_check,
  add constraint comments_status_check
    check (status in ('visible', 'hidden_reports', 'removed', 'removed_by_author')),
  add constraint comments_body_present check (body is not null or status = 'removed_by_author');

-- Listas por cursor (created_at, public_id). A raiz removida pelo autor continua na lista
-- (mostra o aviso e as respostas); oculta por denúncias ou removida pelo admin some com as
-- respostas.
drop index public.comments_feed_idx;
drop index public.comments_link_idx;
drop index public.comments_parent_idx;
create index comments_feed_idx on public.comments (created_at desc, public_id desc)
  where link_id is null and parent_id is null and status in ('visible', 'removed_by_author');
create index comments_link_idx on public.comments (link_id, created_at desc, public_id desc)
  where parent_id is null and status in ('visible', 'removed_by_author');
create index comments_parent_idx on public.comments (parent_id, created_at, public_id)
  where status in ('visible', 'removed_by_author');

-- ===== Suspensão que sobrevive à exclusão da conta (plano §10) =====
-- suspended_until null = banimento permanente; com data = suspenso até ela (a conta recriada
-- com o mesmo Google nasce suspensa até lá).
alter table public.banned_identities add column suspended_until timestamptz;

-- ===== Cooldown de "ofensivo"/"spam" (5 bloqueios sem strike em 24 h → 1 h sem comentar) =====
create index moderation_log_user_blocks_idx on public.moderation_log (user_id, created_at desc)
  where action in ('blocked_local', 'blocked_ai') and category in ('ofensivo', 'spam');

revoke all on all tables    in schema public from anon, authenticated;
revoke all on all sequences in schema public from anon, authenticated;
