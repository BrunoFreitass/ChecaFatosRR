-- ChecaFatosRR — ajustes da moderação (decisões de 13/10/2026).
-- Aplicar depois de 20261013000001_gemini_model_usage.sql (SQL Editor ou `supabase db push`).
-- Antes: ative a extensão pg_cron (Database → Extensions) para a limpeza diária ser agendada.
-- Etapa 9: tabela nova — rode de novo o bloco "do $$ ... $$" de
-- supabase/manual/app_backend_role.sql para a policy do app_backend.

-- 1) Advertências sobrevivem à exclusão da conta por 30 dias. Guardadas pelo HMAC do sub do
--    Google (o mesmo de banned_identities), nunca pelo e-mail ou pelo id da conta. Quem volta
--    com o mesmo Google dentro do prazo recebe as advertências de volta. 5 advertências já
--    são banimento (banned_identities), por isso no máximo 4 aqui.
create table if not exists public.deleted_account_strikes (
  sub_hash   bytea       primary key check (octet_length(sub_hash) = 32),
  strikes    smallint    not null check (strikes between 1 and 4),
  expires_at timestamptz not null,
  created_at timestamptz not null default now()
);
create index if not exists deleted_account_strikes_expires_idx on public.deleted_account_strikes (expires_at);

alter table public.deleted_account_strikes enable row level security;
revoke all on public.deleted_account_strikes from anon, authenticated;

-- 2) Limite de 10 denúncias por pessoa por dia: conta por denunciante na janela de 24 h.
--    O índice novo também cobre a FK reporter_id (substitui o antigo, só com reporter_id).
create index if not exists comment_reports_reporter_recent_idx
  on public.comment_reports (reporter_id, created_at desc);
drop index if exists public.comment_reports_reporter_idx;

-- 3) LGPD: limpeza diária. Suspensões guardadas (conta suspensa excluída) somem 30 dias depois
--    de vencer; advertências guardadas somem no fim do prazo. Banimentos ficam.
create index if not exists banned_identities_suspended_idx on public.banned_identities (suspended_until)
  where suspended_until is not null;

create or replace function public.purge_expired_moderation_records()
returns void
language sql
set search_path = ''
as $$
  delete from public.banned_identities
  where suspended_until is not null and suspended_until < now() - interval '30 days';
  delete from public.deleted_account_strikes where expires_at < now();
$$;
revoke all on function public.purge_expired_moderation_records()
  from public, anon, authenticated, service_role;

-- Todo dia às 03:15 de Boa Vista (07:15 UTC). Sem pg_cron, avisa e segue (agende depois).
do $$
begin
  if exists (select 1 from pg_extension where extname = 'pg_cron') then
    perform cron.schedule(
      'limpeza-lgpd-moderacao', '15 7 * * *', 'select public.purge_expired_moderation_records()'
    );
  else
    raise warning 'pg_cron inativo: ative a extensão e rode o bloco de agendamento de novo.';
  end if;
end
$$;
