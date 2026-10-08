-- ETAPA 9 — papel dedicado do backend (privilégio mínimo).
-- NÃO fica em supabase/migrations: tem senha e é aplicado uma vez, à mão, no SQL Editor (como postgres).
-- Até lá o backend usa o usuário padrão do pooler (postgres.<project_ref>).
--
-- Sem BYPASSRLS: no Supabase o `postgres` não é superuser e normalmente não pode conceder esse
-- atributo. Em vez disso, cada tabela ganha uma policy que libera tudo SÓ para app_backend
-- (anon/authenticated continuam sem acesso). Toda tabela nova precisa da mesma policy:
-- rode de novo o bloco "do $$ ... $$" depois de cada migração que criar tabela.
--
-- Depois de aplicar, troque o DATABASE_URL do backend para (senha com @ / # : precisa de URL-encode):
--   postgresql://app_backend.<project_ref>:<SENHA>@aws-0-<regiao>.pooler.supabase.com:6543/postgres

create role app_backend login password '<TROQUE_POR_UMA_SENHA_FORTE>' connection limit 20;
alter role app_backend set statement_timeout = '15s';

grant usage on schema public to app_backend;
grant select, insert, update, delete on all tables in schema public to app_backend;
grant usage, select on all sequences in schema public to app_backend;
alter default privileges in schema public grant select, insert, update, delete on tables to app_backend;
alter default privileges in schema public grant usage, select on sequences to app_backend;

do $$
declare
  t text;
begin
  for t in select tablename from pg_tables where schemaname = 'public' loop
    execute format('drop policy if exists app_backend_all on public.%I', t);
    execute format(
      'create policy app_backend_all on public.%I for all to app_backend using (true) with check (true)', t
    );
  end loop;
end
$$;

-- moderation_log é append-only para o backend (o expurgo LGPD usa função security definer).
revoke update, delete on public.moderation_log from app_backend;
grant execute on function public.purge_moderation_snapshots(interval) to app_backend;
