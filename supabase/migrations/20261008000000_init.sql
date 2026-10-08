-- ChecaFatosRR — esquema inicial.
-- Aplicar com `supabase db push` ou colando no SQL Editor do Supabase.
-- O frontend nunca acessa estas tabelas: tudo passa pela API (FastAPI).

-- ===== Privilégios: anon/authenticated não recebem nada, nem nas tabelas futuras =====
alter default privileges in schema public revoke all on tables    from anon, authenticated;
alter default privileges in schema public revoke all on sequences from anon, authenticated;
alter default privileges in schema public revoke all on functions from anon, authenticated;
alter default privileges in schema public revoke execute on functions from public;

create extension if not exists citext with schema extensions;

-- ===== Perfis =====
-- Perfil público. Nunca guarda nome real nem e-mail (estes ficam só em auth.users).
create table public.profiles (
  id                uuid primary key references auth.users(id) on delete cascade,
  apelido           extensions.citext not null unique
                    check (char_length(apelido) between 3 and 20 and apelido ~ '^[A-Za-z0-9_]+$'),
  terms_version     text not null,
  terms_accepted_at timestamptz not null default now(),
  status            text not null default 'active' check (status in ('active', 'suspended', 'banned')),
  suspended_until   timestamptz,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now(),
  check (status <> 'suspended' or suspended_until is not null)
);
create index profiles_created_idx on public.profiles (created_at);

-- HMAC-SHA256(BAN_HASH_SALT, sub do Google) de contas banidas: impede voltar com a mesma conta.
-- Removido ao desbanir. Descrito na Política de Privacidade.
create table public.banned_identities (
  sub_hash   bytea primary key check (octet_length(sub_hash) = 32),
  created_at timestamptz not null default now()
);

-- ===== Checagem de links =====
create table public.links (
  id             bigint generated always as identity primary key,
  url_hash       bytea not null unique,  -- sha256(url_normalizada)
  url_normalized text  not null check (char_length(url_normalized) <= 2048),
  domain         text  not null check (char_length(domain) <= 253),
  title          text  check (char_length(title) <= 500),
  published_at   timestamptz,
  created_at     timestamptz not null default now()
);

create table public.checks (
  id         bigint generated always as identity primary key,
  link_id    bigint not null references public.links(id) on delete cascade,
  veredito   text not null
             check (veredito in ('verdadeiro', 'enganoso', 'falso', 'fora_de_contexto', 'nao_verificavel')),
  confianca  smallint not null check (confianca between 0 and 100),
  result     jsonb not null check (pg_column_size(result) < 200000),
  model      text not null,
  created_by uuid references public.profiles(id) on delete set null,
  created_at timestamptz not null default now()
);
create index checks_link_recent_idx on public.checks (link_id, created_at desc);   -- cache 24h
create index checks_recent_idx      on public.checks (created_at desc, id desc);   -- "Últimas" (cursor)
create index checks_created_by_idx  on public.checks (created_by);

-- Rate limit de 10 checagens/hora: a linha entra ANTES da chamada ao Gemini.
create table public.check_requests (
  id         bigint generated always as identity primary key,
  user_id    uuid not null references public.profiles(id) on delete cascade,
  link_id    bigint references public.links(id) on delete set null,
  cache_hit  boolean not null default false,
  created_at timestamptz not null default now()
);
create index check_requests_user_recent_idx on public.check_requests (user_id, created_at desc);
create index check_requests_link_idx        on public.check_requests (link_id);

-- ===== Comentários =====
-- link_id null = feed geral. A resposta tem o mesmo link_id do pai; 1 nível só (trigger).
create table public.comments (
  id           bigint generated always as identity primary key,
  user_id      uuid not null references public.profiles(id) on delete cascade,
  link_id      bigint references public.links(id) on delete cascade,
  parent_id    bigint references public.comments(id) on delete cascade,
  body         text not null check (char_length(body) between 1 and 1000),
  status       text not null default 'visible' check (status in ('visible', 'hidden_reports', 'removed')),
  report_count int  not null default 0 check (report_count >= 0),
  created_at   timestamptz not null default now()
);
create index comments_feed_idx      on public.comments (created_at desc, id desc)
  where link_id is null and parent_id is null and status = 'visible';
create index comments_link_idx      on public.comments (link_id, created_at desc, id desc)
  where parent_id is null and status = 'visible';
create index comments_link_fk_idx   on public.comments (link_id);
create index comments_parent_idx    on public.comments (parent_id, created_at) where status = 'visible';
create index comments_parent_fk_idx on public.comments (parent_id);
create index comments_user_idx      on public.comments (user_id, created_at desc);  -- rate limit 30s

create table public.comment_reports (
  comment_id  bigint not null references public.comments(id) on delete cascade,
  reporter_id uuid   not null references public.profiles(id) on delete cascade,
  reason      text check (char_length(reason) <= 200),
  created_at  timestamptz not null default now(),
  primary key (comment_id, reporter_id)
);
create index comment_reports_reporter_idx on public.comment_reports (reporter_id);

-- ===== Moderação =====
-- Log append-only. content_snapshot guarda o texto bloqueado (que não é salvo em comments);
-- é expurgado após 90 dias e na exclusão da conta (LGPD).
create table public.moderation_log (
  id               bigint generated always as identity primary key,
  user_id          uuid references public.profiles(id) on delete set null,
  comment_id       bigint references public.comments(id) on delete set null,
  actor            text not null check (actor in ('local_filter', 'ai_filter', 'reports', 'admin', 'system')),
  -- blocked_local, blocked_ai, flagged, hidden_by_reports, approved, removed, strike,
  -- strike_revoked, suspended, banned, unbanned, nickname_blocked
  action           text not null check (char_length(action) <= 40),
  -- ok, palavrao, ofensivo, sexual, incitacao_violencia, crime, discurso_de_odio, desinformacao, spam
  category         text check (char_length(category) <= 40),
  content_snapshot text check (char_length(content_snapshot) <= 1000),
  details          jsonb not null default '{}',
  created_at       timestamptz not null default now()
);
create index moderation_log_user_idx    on public.moderation_log (user_id, created_at desc);
create index moderation_log_recent_idx  on public.moderation_log (created_at desc);
create index moderation_log_comment_idx on public.moderation_log (comment_id);

-- Fila do admin (fonte da verdade para "o que precisa de revisão").
--   comment_review:      comentário publicado que precisa de olhar humano
--                        (desinformacao, nao_moderado_ia, hidden_by_reports)
--   strike_confirmation: bloqueio da IA com confiança < 0.85 — o admin confirma ou descarta o strike
create table public.moderation_queue (
  id                bigint generated always as identity primary key,
  kind              text not null check (kind in ('comment_review', 'strike_confirmation')),
  reason            text not null
                    check (reason in ('desinformacao', 'nao_moderado_ia', 'hidden_by_reports', 'ai_low_confidence')),
  user_id           uuid references public.profiles(id) on delete cascade,
  comment_id        bigint references public.comments(id) on delete cascade,
  moderation_log_id bigint references public.moderation_log(id) on delete set null,
  category          text check (char_length(category) <= 40),
  ai_confidence     double precision check (ai_confidence between 0 and 1),
  status            text not null default 'pending' check (status in ('pending', 'resolved')),
  -- approved, removed, strike_confirmed, dismissed
  resolution        text check (char_length(resolution) <= 40),
  resolved_at       timestamptz,
  created_at        timestamptz not null default now(),
  check ((status = 'resolved') = (resolved_at is not null)),
  check ((status = 'resolved') = (resolution is not null)),
  check (
    (kind = 'comment_review'
       and reason in ('desinformacao', 'nao_moderado_ia', 'hidden_by_reports')
       and comment_id is not null)
    or (kind = 'strike_confirmation' and reason = 'ai_low_confidence' and user_id is not null)
  )
);
create index moderation_queue_pending_idx on public.moderation_queue (created_at) where status = 'pending';
create index moderation_queue_user_idx    on public.moderation_queue (user_id);
create index moderation_queue_comment_idx on public.moderation_queue (comment_id);
create index moderation_queue_modlog_idx  on public.moderation_queue (moderation_log_id);
-- Um mesmo comentário não entra duas vezes na fila pelo mesmo motivo enquanto pendente.
create unique index moderation_queue_open_uniq on public.moderation_queue (comment_id, reason)
  where status = 'pending' and comment_id is not null;

create table public.strikes (
  id                bigint generated always as identity primary key,
  user_id           uuid not null references public.profiles(id) on delete cascade,
  category          text not null check (char_length(category) <= 40),
  moderation_log_id bigint references public.moderation_log(id) on delete set null,
  revoked_at        timestamptz,
  created_at        timestamptz not null default now()
);
create index strikes_active_user_idx on public.strikes (user_id) where revoked_at is null;
create index strikes_modlog_idx      on public.strikes (moderation_log_id);

-- ===== Anúncios =====
create table public.ads (
  id         bigint generated always as identity primary key,
  title      text not null check (char_length(title) <= 120),
  image_url  text not null check (char_length(image_url) <= 1024),  -- bucket `ads` (validado no app)
  link_url   text not null check (link_url ~ '^https://[^\s]+$' and char_length(link_url) <= 1024),
  position   text not null check (position in ('top', 'sidebar', 'inline')),
  starts_at  timestamptz not null,
  ends_at    timestamptz not null,
  active     boolean not null default true,
  created_at timestamptz not null default now(),
  check (ends_at > starts_at)
);

-- ===== Apuração TSE =====
-- Último snapshot bom por escopo. fetched_at também serve de "claim" atômico do refresh.
create table public.tse_snapshots (
  scope          text primary key check (scope in ('br', 'rr')),
  payload        jsonb not null,
  tse_updated_at timestamptz,
  fetched_at     timestamptz not null default now()
);

-- ===== Funções e triggers =====
create function public.set_updated_at() returns trigger
language plpgsql security invoker set search_path = '' as $$
begin
  new.updated_at := now();
  return new;
end
$$;
create trigger profiles_updated_at before update on public.profiles
  for each row execute function public.set_updated_at();

-- Resposta em 1 nível + mesmo link do pai; parent_id/link_id imutáveis.
create function public.comments_one_level() returns trigger
language plpgsql security invoker set search_path = '' as $$
declare
  parent record;
begin
  if tg_op = 'UPDATE'
     and (new.parent_id is distinct from old.parent_id or new.link_id is distinct from old.link_id) then
    raise exception 'parent_id e link_id não podem ser alterados';
  end if;
  if new.parent_id is not null then
    select c.parent_id, c.link_id into parent from public.comments c where c.id = new.parent_id;
    if not found or parent.parent_id is not null or parent.link_id is distinct from new.link_id then
      raise exception 'resposta inválida: apenas 1 nível e no mesmo link do comentário original';
    end if;
  end if;
  return new;
end
$$;
create trigger comments_one_level before insert or update of parent_id, link_id on public.comments
  for each row execute function public.comments_one_level();

-- Expurgo LGPD dos textos bloqueados. security definer: funciona mesmo quando o papel
-- do backend não tiver UPDATE em moderation_log (append-only, etapa 9).
-- Preserva textos ainda pendentes de decisão na fila do admin. Mínimo de 1 dia.
create function public.purge_moderation_snapshots(older_than interval default interval '90 days')
returns bigint
language sql security definer set search_path = '' as $$
  with purged as (
    update public.moderation_log l set content_snapshot = null
    where l.content_snapshot is not null
      and l.created_at < now() - greatest(older_than, interval '1 day')
      and not exists (
        select 1 from public.moderation_queue q
        where q.moderation_log_id = l.id and q.status = 'pending'
      )
    returning 1
  )
  select count(*) from purged;
$$;

-- Exclusão de conta (LGPD): apaga os textos bloqueados ANTES do "on delete set null"
-- desvincular o log do usuário. Dispara também na cascata vinda de auth.users.
create function public.profiles_purge_moderation_snapshots() returns trigger
language plpgsql security definer set search_path = '' as $$
begin
  update public.moderation_log set content_snapshot = null
  where user_id = old.id and content_snapshot is not null;
  return old;
end
$$;
create trigger profiles_purge_moderation_snapshots before delete on public.profiles
  for each row execute function public.profiles_purge_moderation_snapshots();

-- ===== RLS: ligada em tudo, sem policies (nega tudo via Data API) =====
alter table public.profiles          enable row level security;
alter table public.banned_identities enable row level security;
alter table public.links             enable row level security;
alter table public.checks            enable row level security;
alter table public.check_requests    enable row level security;
alter table public.comments          enable row level security;
alter table public.comment_reports   enable row level security;
alter table public.moderation_log    enable row level security;
alter table public.moderation_queue  enable row level security;
alter table public.strikes           enable row level security;
alter table public.ads               enable row level security;
alter table public.tse_snapshots     enable row level security;

revoke all on all tables    in schema public from anon, authenticated;
revoke all on all sequences in schema public from anon, authenticated;
revoke all on all functions in schema public from public, anon, authenticated, service_role;
