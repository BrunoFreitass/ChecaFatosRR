-- ChecaFatosRR — etapa 2: checagem de links e de textos colados.
-- Aplicar depois de 20261008000000_init.sql (SQL Editor ou `supabase db push`).
-- Etapa 9: tabelas novas precisam da policy do app_backend — rode de novo o bloco
-- "do $$ ... $$" de supabase/manual/app_backend_role.sql.

-- ===== Textos colados (mensagens encaminhadas) =====
-- O corpo já chega mascarado (telefone, e-mail, CPF, Pix) pelo backend.
create table public.texts (
  id             bigint generated always as identity primary key,
  text_hash      bytea not null unique check (octet_length(text_hash) = 32),  -- sha256(texto normalizado)
  body           text  not null check (char_length(body) between 1 and 5000),
  checking_until timestamptz,  -- claim do single-flight (uma chamada ao Gemini por texto)
  created_at     timestamptz not null default now()
);

alter table public.links add column checking_until timestamptz;  -- idem, por link
-- A data de publicação vem da página só como dia (AAAA-MM-DD): timestamptz deslocaria o dia
-- no fuso de Roraima. A tabela ainda está vazia.
alter table public.links alter column published_at type date using published_at::date;

-- ===== Checagens: assunto é um link OU um texto; id público aleatório =====
-- public_id: 12 caracteres base32 (Crockford, minúsculo), gerado no backend. O bigint é só
-- interno e nunca aparece em URL ou resposta (sem enumeração).
-- "not null" direto: ainda não há checagens gravadas (a etapa 2 é a primeira a gravar).
alter table public.checks
  alter column link_id drop not null,
  add column text_id   bigint references public.texts(id) on delete cascade,
  add column public_id text not null,
  add column listed    boolean not null default true,
  add constraint checks_public_id_key unique (public_id),
  add constraint checks_public_id_format check (public_id ~ '^[0-9a-hjkmnp-tv-z]{12}$'),
  add constraint checks_one_subject check (num_nonnulls(link_id, text_id) = 1),
  -- Checagem de texto nunca aparece em "Últimas" (só por quem tiver o link).
  add constraint checks_text_unlisted check (text_id is null or not listed);

create index checks_text_recent_idx on public.checks (text_id, created_at desc)
  where text_id is not null;                                                  -- cache 24h
create index checks_listed_idx on public.checks (created_at desc, public_id desc)
  where listed;                                                               -- "Últimas"
-- Substituído por checks_listed_idx (o cursor público usa public_id, não o id interno).
drop index public.checks_recent_idx;

alter table public.check_requests
  add column text_id bigint references public.texts(id) on delete set null;
create index check_requests_text_idx on public.check_requests (text_id);

-- ===== Cota diária do Gemini (checagem e moderação separadas) =====
-- day = dia no fuso do Pacífico: é quando a cota gratuita do Google zera.
-- Reserva atômica: insert ... on conflict do update ... where calls < limite returning.
create table public.gemini_usage (
  day     date    not null,
  purpose text    not null check (purpose in ('checagem', 'moderacao')),
  calls   integer not null default 0 check (calls >= 0),
  primary key (day, purpose)
);

-- ===== RLS: ligada, sem policies (nega tudo via Data API), como as demais =====
alter table public.texts        enable row level security;
alter table public.gemini_usage enable row level security;

revoke all on public.texts, public.gemini_usage from anon, authenticated;
revoke all on all sequences in schema public from anon, authenticated;
