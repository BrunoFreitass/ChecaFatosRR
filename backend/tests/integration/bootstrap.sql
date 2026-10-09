-- Simula, num Postgres puro (CI), o que o Supabase já tem antes da nossa migração.
-- Só as colunas que o ChecaFatosRR usa. NUNCA aplicar isto num projeto Supabase.

create schema if not exists auth;
create schema if not exists extensions;

create table if not exists auth.users (
  id uuid primary key
);

create table if not exists auth.identities (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null references auth.users(id) on delete cascade,
  provider    text not null,
  provider_id text not null,
  unique (provider_id, provider)
);

do $$
begin
  create role anon nologin;
exception when duplicate_object then null;
end
$$;
do $$
begin
  create role authenticated nologin;
exception when duplicate_object then null;
end
$$;
do $$
begin
  create role service_role nologin;
exception when duplicate_object then null;
end
$$;
