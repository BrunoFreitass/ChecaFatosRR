# Plano: ChecaFatosRR — MVP (2º turno 2026)

**Complexidade**: Grande · **Prazo**: no ar o quanto antes; precisa funcionar até a divulgação do resultado (25/10/2026)
**Base de código**: diretório vazio (greenfield) — não há padrões existentes para espelhar; os padrões abaixo passam a ser a convenção.

## 1. Decisões de arquitetura

| Tema | Decisão | Motivo |
|---|---|---|
| Acesso a dados | Frontend usa `supabase-js` **só para login**. Todo dado passa pela API FastAPI. | Uma única camada de validação/moderação; RLS simples (deny-all). |
| ORM | SQLAlchemy 2 async + `asyncpg`, conexão pelo **Supavisor (pooler) em modo transaction** (`statement_cache_size=0`). | Cloud Run escala a zero; o pooler evita esgotar conexões. |
| Migrações | SQL puro em `supabase/migrations/*.sql` (Supabase CLI ou SQL Editor). Sem Alembic. | Menos peças; RLS/policies/triggers são SQL de qualquer jeito. |
| RLS | RLS **ligada em todas as tabelas, sem policies** para `anon`/`authenticated` (= nega tudo via PostgREST). O backend conecta com papel dedicado `app_backend` (BYPASSRLS). | Impede uso direto da chave anon contra as tabelas. |
| Auth no backend | Valida o JWT do Supabase via **JWKS** (`/auth/v1/.well-known/jwks.json`, chaves assimétricas), com fallback HS256 (`SUPABASE_JWT_SECRET`) para projetos legados. | Projetos novos do Supabase usam chaves assimétricas. |
| Admin | `ADMIN_EMAIL` no `.env`; confere o claim `email` + `email_verified` do JWT. Sem coluna de papel no banco. | Pedido explícito: "só meu e-mail". |
| Rate limit | Contagem no Postgres (índices em `user_id, created_at`). Sem Redis. | Custo zero; volume baixo. |
| Cache de checagem | Hash SHA-256 da URL normalizada → `links`; reaproveita a última `checks` se tiver < 24h. | content-hash-cache-pattern. |
| Gemini | SDK `google-genai`, `response_schema` = modelo Pydantic. `GEMINI_MODEL` (Flash) para checagem; `GEMINI_MODERATION_MODEL` (Flash-Lite) para moderação. | cost-aware-llm-pipeline: modelo mais barato para classificar. |
| Moderação | Camada 1 regex/normalização local → só passa para a IA o que foi aprovado localmente → camada 3 denúncias. | regex-vs-llm: o filtro local barato fica na frente. |
| Apuração TSE | **Refresh preguiçoso com single-flight**: a requisição ao nosso endpoint dispara uma busca no TSE se o cache tiver > 30s (com lock; os demais recebem o cache). O último snapshot bom fica em `tse_snapshots`. Loop opcional em segundo plano quando `TSE_BACKGROUND_POLL=true`. | Funciona com scale-to-zero (custo zero); o loop só faz sentido com min-instances=1 (pago). |
| Anúncios (imagem) | Bucket público `ads` no Supabase Storage; upload feito pelo backend (service role). | Custo zero, CDN. |
| Deploy | Backend: Dockerfile → `gcloud run deploy --source` (Cloud Build; não precisa de Docker local). Render como plano B (mesmo Dockerfile). Frontend: Cloudflare Pages (`npm run build`, `dist/`). | Pedido. |

## 2. Estrutura de pastas (monorepo)

```
/
├─ backend/
│  ├─ app/
│  │  ├─ main.py               # FastAPI, CORS restrito, handlers de erro amigáveis
│  │  ├─ config.py             # pydantic-settings (todas as envs)
│  │  ├─ db.py                 # engine async + sessão
│  │  ├─ logging.py            # logs JSON estruturados (structlog)
│  │  ├─ auth.py               # verificação JWT (JWKS/HS256), deps current_user / require_admin
│  │  ├─ models.py             # ORM SQLAlchemy
│  │  ├─ schemas/              # Pydantic de entrada/saída (profiles, checks, comments, ads, tse, admin)
│  │  ├─ routers/              # health, profiles, checks, comments, reports, tse, ads, admin
│  │  └─ services/
│  │     ├─ url_guard.py       # normalização + anti-SSRF  [TESTE OBRIGATÓRIO]
│  │     ├─ fetcher.py         # httpx: timeout 10s, limite 2MB, redirects revalidados
│  │     ├─ extractor.py       # trafilatura
│  │     ├─ factcheck.py       # Google Fact Check Tools (claims:search, pt)
│  │     ├─ gemini.py          # cliente + schema do veredito  [TESTE OBRIGATÓRIO]
│  │     ├─ check_service.py   # orquestração + cache 24h + rate limit
│  │     ├─ rate_limit.py
│  │     ├─ moderation/
│  │     │  ├─ local_filter.py # normalização leetspeak/acentos/repetições  [TESTE OBRIGATÓRIO]
│  │     │  ├─ wordlist_pt.txt
│  │     │  ├─ ai_filter.py
│  │     │  └─ strikes.py      # 3 → 24h, 5 → ban  [TESTE OBRIGATÓRIO]
│  │     └─ tse/
│  │        ├─ adapter.py      # URLs configuráveis por .env
│  │        ├─ parser.py       # JSON TSE → modelo interno  [TESTE OBRIGATÓRIO]
│  │        └─ service.py      # cache, single-flight, persistência
│  ├─ tests/
│  │  ├─ unit/                 # url_guard, local_filter, strikes, tse_parser, gemini_schema
│  │  ├─ smoke/                # endpoints principais com TestClient
│  │  └─ fixtures/tse_2022/    # br e rr do 2º turno de 2022 (eleição 545)
│  ├─ Dockerfile               # python:3.12-slim, usuário não-root, uvicorn em $PORT
│  ├─ pyproject.toml           # deps + ruff + pytest
│  └─ .env.example
├─ frontend/
│  ├─ src/
│  │  ├─ main.tsx, App.tsx, router.tsx
│  │  ├─ lib/                  # supabase.ts (só auth), api.ts (fetch + token), format.ts
│  │  ├─ hooks/                # useAuth, useProfile, usePolling
│  │  ├─ components/           # Layout, VerdictBadge, AdSlot, CommentList, CommentForm,
│  │  │                        # ViolationNotice, ReportButton, TseOfficialBadge
│  │  └─ pages/                # Home(checar), Resultado, Ultimas, Feed, Apuracao,
│  │                           # Onboarding(apelido+termos), admin/*, legal/{Termos,Privacidade,Sobre}
│  ├─ e2e/                     # Playwright: checar link, comentar, aviso de violação, apuração
│  ├─ public/_redirects        # fallback SPA no Cloudflare Pages
│  ├─ vite.config.ts, tailwind config, package.json
│  └─ .env.example             # VITE_SUPABASE_URL, VITE_SUPABASE_ANON_KEY, VITE_API_URL
├─ supabase/migrations/        # 0001_init.sql, ...
├─ docs/DEPLOY.md
└─ README.md
```

## 3. Modelo de dados (`0001_init.sql`, revisado pelo database-reviewer)

Valores de domínio que podem mudar durante o prazo (veredito, categoria, ação, status) usam **`text` + `check`** em vez de enum. Assim evitamos o atrito do asyncpg com o pooler e o `ALTER TYPE`.

```sql
create extension if not exists citext with schema extensions;

-- Perfil público. Nunca guarda nome real nem e-mail (estes ficam só em auth.users).
create table profiles (
  id                uuid primary key references auth.users(id) on delete cascade,
  apelido           extensions.citext not null unique
                    check (char_length(apelido) between 3 and 20 and apelido ~ '^[A-Za-z0-9_]+$'),
  terms_version     text not null,
  terms_accepted_at timestamptz not null default now(),
  status            text not null default 'active' check (status in ('active','suspended','banned')),
  suspended_until   timestamptz,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now(),
  check (status <> 'suspended' or suspended_until is not null)
);
create index profiles_created_idx on profiles (created_at);

create table links (
  id             bigint generated always as identity primary key,
  url_hash       bytea not null unique,                  -- sha256(url_normalizada)
  url_normalized text  not null check (char_length(url_normalized) <= 2048),
  domain         text  not null check (char_length(domain) <= 253),
  title          text  check (char_length(title) <= 500),
  published_at   timestamptz,
  created_at     timestamptz not null default now()
);

create table checks (
  id          bigint generated always as identity primary key,
  link_id     bigint not null references links(id) on delete cascade,
  veredito    text not null check (veredito in ('verdadeiro','enganoso','falso','fora_de_contexto','nao_verificavel')),
  confianca   smallint not null check (confianca between 0 and 100),
  result      jsonb not null check (pg_column_size(result) < 200000),
  model       text not null,
  created_by  uuid references profiles(id) on delete set null,
  created_at  timestamptz not null default now()
);
create index checks_link_recent_idx on checks (link_id, created_at desc);          -- cache 24h
create index checks_recent_idx      on checks (created_at desc, id desc);          -- "Últimas" (cursor)
create index checks_created_by_idx  on checks (created_by);

-- Rate limit 10/h: é inserido ANTES de chamar o Gemini (pedidos que falham também contam).
create table check_requests (
  id         bigint generated always as identity primary key,
  user_id    uuid not null references profiles(id) on delete cascade,
  link_id    bigint references links(id) on delete set null,
  cache_hit  boolean not null default false,
  created_at timestamptz not null default now()
);
create index check_requests_user_recent_idx on check_requests (user_id, created_at desc);
create index check_requests_link_idx        on check_requests (link_id);

-- link_id null = feed geral. A resposta herda o link_id do pai; 1 nível só (trigger).
create table comments (
  id           bigint generated always as identity primary key,
  user_id      uuid not null references profiles(id) on delete cascade,
  link_id      bigint references links(id) on delete cascade,
  parent_id    bigint references comments(id) on delete cascade,
  body         text not null check (char_length(body) between 1 and 1000),
  status       text not null default 'visible' check (status in ('visible','hidden_reports','removed')),
  report_count int  not null default 0,
  created_at   timestamptz not null default now()
);
create index comments_feed_idx      on comments (created_at desc, id desc) where link_id is null and parent_id is null and status = 'visible';
create index comments_link_idx      on comments (link_id, created_at desc, id desc) where parent_id is null and status = 'visible';
create index comments_link_fk_idx   on comments (link_id);
create index comments_parent_idx    on comments (parent_id, created_at) where status = 'visible';
create index comments_parent_fk_idx on comments (parent_id);
create index comments_user_idx      on comments (user_id, created_at desc);        -- rate limit 30s
create index comments_hidden_idx    on comments (created_at) where status = 'hidden_reports';  -- fila admin

create table comment_reports (
  comment_id  bigint not null references comments(id) on delete cascade,
  reporter_id uuid   not null references profiles(id) on delete cascade,
  reason      text check (char_length(reason) <= 200),
  created_at  timestamptz not null default now(),
  primary key (comment_id, reporter_id)
);
create index comment_reports_reporter_idx on comment_reports (reporter_id);

create table moderation_log (
  id               bigint generated always as identity primary key,
  user_id          uuid references profiles(id) on delete set null,
  comment_id       bigint references comments(id) on delete set null,
  actor            text not null check (actor in ('local_filter','ai_filter','reports','admin','system')),
  action           text not null,  -- blocked_local, blocked_ai, hidden_by_reports, approved, removed,
                                   -- strike, suspended, banned, unbanned, strike_revoked, nickname_blocked
  category         text,           -- ok, palavrao, ofensivo, sexual, incitacao_violencia, crime,
                                   -- discurso_de_odio, desinformacao, spam
  content_snapshot text check (char_length(content_snapshot) <= 1000),
  details          jsonb not null default '{}',
  created_at       timestamptz not null default now()
);
create index moderation_log_user_idx    on moderation_log (user_id, created_at desc);
create index moderation_log_recent_idx  on moderation_log (created_at desc);
create index moderation_log_comment_idx on moderation_log (comment_id);

create table strikes (
  id                bigint generated always as identity primary key,
  user_id           uuid not null references profiles(id) on delete cascade,
  category          text not null,
  moderation_log_id bigint references moderation_log(id) on delete set null,
  revoked_at        timestamptz,
  created_at        timestamptz not null default now()
);
create index strikes_active_user_idx on strikes (user_id) where revoked_at is null;
create index strikes_modlog_idx      on strikes (moderation_log_id);

create table ads (
  id         bigint generated always as identity primary key,
  title      text not null check (char_length(title) <= 120),
  image_url  text not null check (char_length(image_url) <= 1024),  -- precisa apontar para o bucket `ads` (validado no app)
  link_url   text not null check (link_url ~ '^https://[^\s]+$' and char_length(link_url) <= 1024),
  position   text not null check (position in ('top','sidebar','inline')),
  starts_at  timestamptz not null,
  ends_at    timestamptz not null,
  active     boolean not null default true,
  created_at timestamptz not null default now(),
  check (ends_at > starts_at)
);

create table tse_snapshots (
  scope          text primary key check (scope in ('br','rr')),
  payload        jsonb not null,
  tse_updated_at timestamptz,
  fetched_at     timestamptz not null default now()
);

-- updated_at automático
create function set_updated_at() returns trigger language plpgsql security invoker set search_path = '' as $$
begin new.updated_at := now(); return new; end $$;
create trigger profiles_updated_at before update on profiles for each row execute function set_updated_at();

-- Resposta em 1 nível + mesmo link do pai; parent_id/link_id imutáveis.
create function comments_one_level() returns trigger language plpgsql security invoker set search_path = '' as $$
declare p record;
begin
  if tg_op = 'UPDATE' and (new.parent_id is distinct from old.parent_id or new.link_id is distinct from old.link_id) then
    raise exception 'parent_id/link_id imutáveis';
  end if;
  if new.parent_id is not null then
    select parent_id, link_id into p from public.comments where id = new.parent_id;
    if not found or p.parent_id is not null or p.link_id is distinct from new.link_id then
      raise exception 'resposta inválida (apenas 1 nível, mesmo link)';
    end if;
  end if;
  return new;
end $$;
create trigger comments_one_level before insert or update of parent_id, link_id on comments
  for each row execute function comments_one_level();

-- ===== Segurança =====
alter table profiles        enable row level security;
alter table links           enable row level security;
alter table checks          enable row level security;
alter table check_requests  enable row level security;
alter table comments        enable row level security;
alter table comment_reports enable row level security;
alter table moderation_log  enable row level security;
alter table strikes         enable row level security;
alter table ads             enable row level security;
alter table tse_snapshots   enable row level security;

revoke all on all tables    in schema public from anon, authenticated;
revoke all on all sequences in schema public from anon, authenticated;
revoke all on all functions in schema public from anon, authenticated;
alter default privileges in schema public revoke all on tables    from anon, authenticated;
alter default privileges in schema public revoke all on sequences from anon, authenticated;
alter default privileges in schema public revoke all on functions from anon, authenticated;

-- Papel do backend (criar no SQL Editor como postgres; a senha fica só no .env do backend).
-- Conexão: usuário `app_backend.<project_ref>`, porta 6543 (transaction mode).
--   create role app_backend login password '<SENHA>' bypassrls;
grant usage on schema public to app_backend;
grant select, insert, update, delete on all tables in schema public to app_backend;
grant usage, select on all sequences in schema public to app_backend;
alter default privileges in schema public grant select, insert, update, delete on tables to app_backend;
alter default privileges in schema public grant usage, select on sequences to app_backend;
revoke update, delete on moderation_log from app_backend;   -- append-only
-- Expurgo LGPD do snapshot: função security definer purge_moderation_snapshots(older_than interval).
```

Além disso: **desligar a Data API (PostgREST) para o schema `public`** em Settings, já que o frontend nunca a usa. O bucket `ads` é criado com `allowed_mime_types` (png/jpeg/webp) e `file_size_limit` de 500 KB.

**Regras de concorrência e de negócio**
- **Lock único por usuário**: todo fluxo que mexe em rate limit, strikes ou status começa com `select ... from profiles where id = $1 for update`. A ordem de lock é sempre a mesma, então não há deadlock nem estouro do limite por requisições paralelas.
- **Strikes**: inserir o strike e recalcular o status numa instrução só: ≥5 ativos → `banned`; ≥3 → `suspended` + 24h (o 4º strike renova as 24h); `strike_revoked`/`unbanned` reavaliam o status. Suspensão vencida conta como ativa na leitura (sem cron).
- **Denúncias**: `with ins as (insert ... on conflict do nothing returning 1) update comments ... from ins`. Só incrementa quando a denúncia é nova. O app bloqueia autodenúncia e denúncias de suspensos/banidos.
- **Comentário raiz removido ou oculto**: as respostas continuam no banco, mas a API não as exibe.
- **Apelido**: `citext unique` + captura do erro `23505` → 409. `/apelido/disponivel` é só uma dica. Nomes reservados (admin, moderador, checafatos, tse…) ficam no app.
- **Cache de checagem**: upsert do link com `on conflict (url_hash)`; `pg_try_advisory_xact_lock` curto por link para evitar duas chamadas ao Gemini. **Nunca** segurar a transação aberta durante chamadas externas.
- **TSE single-flight entre instâncias**: claim atômico `update tse_snapshots set fetched_at = now() where scope = $1 and fetched_at < now() - interval '30 seconds' returning 1`. Quem não pega o claim serve o cache. O upsert só grava se `tse_updated_at` for mais novo.
- **Cursor de paginação**: `(created_at, id)`.
- **LGPD / exclusão de conta**: `DELETE /me` zera `content_snapshot` do usuário, chama a Admin API do Supabase (`auth.admin.delete_user`) e a cascata apaga o resto.
- **Métricas admin**: `count(*)` sobre as tabelas existentes. Não precisa de tabela própria.

## 4. API (resumo)

`GET /health` · `GET/POST/DELETE /me` (perfil + apelido + termos; exclusão LGPD) · `GET /apelido/disponivel?q=`
`POST /checks {url}` · `GET /checks/{id}` · `GET /checks/latest?cursor=`
`GET /comments?link_id=&cursor=` · `POST /comments` · `POST /comments/{id}/report`
`GET /tse/resultado?escopo=br|rr` · `GET /ads?position=`
`/admin/*`: fila de moderação (aprovar/remover), usuários com strikes (banir/desbanir/revogar strike), CRUD de anúncios + upload, métricas.
Erros no formato `{ "error": { "code", "message" } }` com mensagem em pt-BR; violação retorna 422 com `{code:"violacao", regra, explicacao, strikes, status}`.

## 5. Apuração TSE — formato conhecido (a confirmar na etapa 5)
2022, 2º turno: `https://resultados.tse.jus.br/oficial/ele2022/545/dados-simplificados/{br|rr}/{br|rr}-c0001-e000545-r.json`
Campos: `pst` (% seções totalizadas), `dg`/`hg` (data/hora), `cand[]` (`nm`, `n`, `vap`, `pvap`), `vb`/`pvb` (brancos), `tvn`/`ptvn` (nulos), `a`/`pa` (abstenções). Envs: `TSE_BASE_URL`, `TSE_ELECTION_CODE`, `TSE_CARGO`, `TSE_URL_TEMPLATE`. O código da eleição de 2026 só será confirmado quando o TSE publicar; por isso o adaptador é configurável.

## 6. Etapas (cada uma: plan → testes críticos → implementar → review → verify)

| # | Etapa | Testes obrigatórios | Complexidade |
|---|---|---|---|
| 0 | Esqueleto monorepo, `0001_init.sql`, config, logs, health, Dockerfile, CI de lint/teste, DEPLOY.md | smoke `/health` | Baixa |
| 1 | Auth Google + onboarding (apelido + termos) | filtro de apelido (reusa local_filter) | Média |
| 2 | Checagem de links | url_guard/SSRF, schema Gemini | Alta |
| 3 | Comentários (feed + vinculados, 1 nível, 30s) | smoke + e2e | Média |
| 4 | Moderação 3 camadas + strikes + aviso de violação | local_filter, strikes | Alta |
| 5 | Apuração TSE (BR + RR) | parser com fixtures de 2022 | Média |
| 6 | Anúncios + `<AdSlot>` | smoke | Baixa |
| 7 | Painel admin | smoke + e2e | Média |
| 8 | Páginas legais | — | Baixa |
| 9 | Deploy (Cloud Run + Pages), /security-scan, e2e em produção | e2e | Média |

**Dependências**: 0 → 1 → (2, 3) → 4 → 7; a etapa 5 é independente e **crítica no tempo** (precisa estar validada antes de 25/10).

## 7. Riscos

| Risco | Nível | Mitigação |
|---|---|---|
| Prazo (17 dias) | ALTO | Etapas pequenas; deploy contínuo a partir da etapa 1; apuração TSE com data fixa. |
| Formato/URL do TSE em 2026 diferente de 2022 | ALTO | Adaptador por .env, parser tolerante, validação no ambiente simulado do TSE assim que sair. Snapshot persistido serve o último dado bom. |
| Cota gratuita do Gemini estourar (checagem + moderação) | ALTO | Cache 24h, rate limit, Flash-Lite na moderação, filtro local na frente, texto truncado (~8k chars). Fallback definido (ver pergunta 1). |
| SSRF por DNS rebinding/redirect | MÉDIO | Resolver DNS, validar todos os IPs, conectar ao IP validado e revalidar cada redirect (máx. 3). |
| Moderação/IA gerar conteúdo com viés político | MÉDIO | Prompt apartidário, temperatura baixa, fontes apenas da Fact Check API ou do texto (validação pós-resposta remove URLs não permitidas). |
| Legislação eleitoral (anúncios, enquetes) | MÉDIO | Sem enquetes; anúncios cadastrados só pelo admin com aceite da regra "sem conteúdo político". |
| Cold start no Cloud Run na noite da apuração | BAIXO | Imagem slim; opcionalmente min-instances=1 só em 25/10. |

## 8. Perguntas antes de começar
1. **Gemini fora do ar/sem cota na moderação**: (a) *fail-open* — publica com filtro local + denúncias e registra no log (recomendado); ou (b) *fail-closed* — rejeita com "tente mais tarde".
2. **Apelido**: só `[A-Za-z0-9_]` ou permitir acentos? (Recomendo sem acentos: evita homógrafos.)
3. Você já tem projeto no Supabase, no GCP (Cloud Run + chave Fact Check + Gemini) e conta no Cloudflare?
4. **Banimento × recriar conta**: ao excluir a conta de um banido, guardar um hash (com salt) do `sub` do Google para impedir que ele volte? É dado pseudonimizado e precisa constar na Política de Privacidade. (Recomendo: sim.)
5. **Denúncias**: 3 contas novas conseguem ocultar qualquer comentário. Exigir conta com mais de 24h para denunciar? (Recomendo: sim.)

## 9. Ajustes aprovados (08/10/2026)

**Nova ordem**: 0 → 1 → 2 → **deploy público** → 5 (TSE) → 3+4 juntas → 6 → 7 → 8 → 9. Comentários só vão para produção junto com a moderação.

**Moderação**
- `desinformacao`: não bloqueia nem dá strike; o comentário fica visível e entra em `moderation_queue` (reason `desinformacao`).
- Strike automático só para: `palavrao` (filtro local), `sexual`, `crime`, `discurso_de_odio`, `incitacao_violencia`.
- Bloqueio pela IA sempre mostra o aviso de violação. Com confiança >= 0.85 o strike é automático; abaixo disso entra em `moderation_queue` (`strike_confirmation`, reason `ai_low_confidence`) para o admin confirmar.
- Gemini indisponível: fail-open; o comentário é publicado e entra na fila com reason `nao_moderado_ia`.
- Denunciar exige conta com mais de 24h.
- Banimento grava `HMAC-SHA256(BAN_HASH_SALT, sub do Google)` em `banned_identities` (removido ao desbanir); consta na Política de Privacidade.

**Banco**
- Pooler em transaction mode: `NullPool`, `statement_cache_size=0`, `prepared_statement_cache_size=0` e nomes únicos de prepared statements.
- Etapas 0–8 usam o usuário padrão do pooler (`postgres.<ref>`). O papel `app_backend` fica para a etapa 9 (`supabase/manual/app_backend_role.sql`).
- Tabelas novas: `moderation_queue` e `banned_identities`. O índice `comments_hidden_idx` sai; a fila passa a ser a fonte da verdade.

**Respostas**: 1 fail-open + fila `nao_moderado_ia` · 2 apelido `[A-Za-z0-9_]` · 3 *pendente (contas Supabase/GCP/Cloudflare)* · 4 sim · 5 sim.

**DEPLOY.md**: incluir os comandos para min-instances=1 em 25/10 e para voltar a 0 depois.

## 10. Decisões após a etapa 1 (08/10/2026)
- **Etapa 4 (definido pelo usuário):** ao excluir a conta de um usuário **suspenso**, guardar o hash do `sub` do Google junto com a **data de fim da suspensão**. Se ele recriar a conta com o mesmo Google, a suspensão continua valendo até essa data; a punição não zera. Implementação prevista: coluna `suspended_until` em `banned_identities` (nula = banimento permanente), checada no `POST /me` e na criação do perfil.
- **Contato e encarregado de dados (LGPD):** checafatosrr@gmail.com.
- **Pendente:** o teste manual da etapa 1 foi aprovado sem os resultados registrados. A exclusão de conta via Admin API com a chave `sb_secret_` não está verificada.

## 11. Etapa 2 (09/10/2026)
Plano: `.claude/plans/etapa-2-checagem.plan.md`. Mudanças em relação a este plano:
- **Texto colado** entra junto com os links: nova tabela `texts`; `checks` passa a ter `link_id` **ou** `text_id`.
- **Limites**: 60/h de consultas ao cache, além das 10/h de checagens novas.
- **Cotas diárias do Gemini** separadas (`GEMINI_DAILY_CHECKS` / `GEMINI_DAILY_MODERATION`), na tabela `gemini_usage`.
- **Confiança < 40** vira `nao_verificavel`, mantendo a explicação.
- **Checagens de texto não aparecem em "Últimas"**: o acesso é só pelo link compartilhado (`noindex`).
- **Identificador público aleatório** (`checks.public_id`, 12 caracteres base32, 60 bits) para links e textos. O `bigint` é só interno, nunca aparece em rota, resposta ou cursor. **Vale também para as próximas etapas**: comentários, anúncios e admin não expõem ids sequenciais em URL pública.
- **Etapa 3 (decidido pelo usuário):** **sem comentários em checagens de texto no MVP**. Comentários só em links e no feed geral.

## 12. Prompt e cache da checagem (10/10/2026)
- **Versão do prompt na chave do cache:** `gemini.PROMPT_VERSION` (revisão manual + hash das instruções e do schema; o teste de "retrato" obriga a subir `PROMPT_REVISION` quando o pedido montado muda). Resultado de outra versão não é servido como cache; continua no link público, com o aviso "Análise anterior".
- **Sem cota do Gemini:** serve a análise mais recente do mesmo conteúdo (qualquer versão, dentro da janela do cache), com o aviso "Análise anterior". Continua contando como checagem nova (limites de 10/h e 20/dia), para não virar consulta ilimitada. Sem análise anterior: 503 "esgotada hoje".
- **"Últimas":** um item por link (a checagem listada mais recente).
- **PROMPT CONGELADO a partir de 22/10/2026** (decisão do usuário). De 22/10 até o fim do 2º turno (25/10), nada de mudar `SYSTEM_INSTRUCTION`, `build_prompt`, `finalize`, o schema, `PROMPT_REVISION` ou `GEMINI_MODEL_FALLBACKS`. Motivo: mudar a versão invalida o cache inteiro de uma vez e, na reta final, a demanda por checagens novas gastaria a cota diária. Exceção só com decisão explícita do usuário.

## 13. Páginas legais: pendências registradas (10/10/2026)
- **Gemini no nível gratuito:** pelos termos da API do Gemini, no uso gratuito ("Unpaid Services") o Google pode usar o conteúdo enviado e as respostas para melhorar os produtos dele, inclusive com revisão humana. A **Política de Privacidade** precisa dizer isso com clareza:
  - o link e o texto colado são enviados ao Google (Gemini) para a análise;
  - podem ser usados pelo Google nessas condições;
  - não cole dados pessoais. O app já mascara telefone, e-mail e CPF no texto colado, mas o aviso continua necessário.
  - Conferir o texto vigente dos termos na data da redação.
- O projeto da chave do Gemini fica **sem faturamento** de propósito (ver `deploy.plan.md`, fase 1). Se um dia passar ao nível pago, revisar a Política (o uso pelo Google muda).

## 14. Congelamento até o 2º turno e itens pós-eleição (13/10/2026)

**Regras até a eleição (decisão do usuário, 13/10/2026):**
- Até 21/10: só correções de bugs e itens já previstos neste plano (inclui o teste de carga leve).
- A partir de 22/10: prompt da IA congelado (ver §12).
- Em 25/10 (2º turno): nenhum deploy.

**Exceções ao congelamento (aprovadas pelo usuário em 14/10/2026; plano em `mapa-e-painel-admin.plan.md`, aguardando aprovação do plano):**
- **A) Mapa do Brasil na Apuração:** as 27 UFs coloridas pelo líder, com a margem, um painel de detalhe e uma tabela acessível. Usa a tabela nova `tse_uf_snapshots` (migration), a chave `TSE_MAPA_ENABLED` e não mexe no Gemini. Prazo: **18/10**, validado no ensaio com `TSE_ELECTION_CODE=6257`.
- **B) Painel mínimo do admin** (só leitura, agregados, sem rastrear tempo por pessoa), mais o Cloudflare Web Analytics, com ajuste na CSP. Prazo: **20/10**. Se apertar, B é cortado antes de A.
- **C) Menu de acessibilidade próprio** (exceção aprovada pelo usuário em 16/10/2026; só frontend). Prazo: **18/10**.
  - **Opções:** fonte maior ou menor em passos de rem; alto contraste, inverter cores e tons de cinza; sublinhar links; espaçamento WCAG 1.4.12; pausar animações (respeita `prefers-reduced-motion`); cursor grande; "Ouvir" (Web Speech API, só voz pt-BR do aparelho); restaurar padrão.
  - **Restrições:** código e design nossos, sem script de terceiros; CSP e Política de Privacidade inalteradas. Preferências no localStorage, aplicadas antes da 1ª pintura por `public/a11y-init.js`.
  - **Peso:** o painel é um chunk separado.
  - **Fora do escopo:** descrição de imagens e resumo da página por IA (Gemini), e o VLibras (ver pós-eleição).
  - **Diferencial de inclusão:** a acessibilidade é um dos diferenciais de inclusão do projeto. O projeto será inscrito na **FAPERR (edital 002/2026)** como "ChecaFatosRR".

**Pós-eleição (decidido NÃO fazer antes de 25/10):**
- Função PL/pgSQL para o `begin_check`: faria os 3 comandos (lock do usuário + assunto; uso, cache e claim; pedido + claim) numa só chamada. Cada comando interno teria o próprio snapshot, então a garantia dos limites por conta se mantém. Medido localmente em 13/10, a 100–120 ms por ida e volta: `begin_check` ~1,55 s e `db_ms` ~2,3 s no caminho normal. A estimativa com a função é `begin_check` ~0,7 s e `db_ms` ~1,4 s.
  - **Por que fica para depois:** o ganho (~0,9 s numa checagem de 5–30 s) não justifica o risco nos limites por conta, nem uma migration a 12 dias do 2º turno.
  - **Cuidados quando for feita:**
    - declarar a função VOLATILE, SECURITY INVOKER e com `search_path` fixo;
    - `revoke execute` de `anon`, `authenticated` e `public`;
    - testes de integração com requisições simultâneas;
    - migration acompanhada do alerta de migration pendente e de uma marca no `/health/ready`.
- Juntar os comandos 1 e 2 do `begin_check` num SQL comum é **inseguro**: em READ COMMITTED, o snapshot seria tirado antes do lock do usuário. Não fazer.
- **VLibras** (tradução para Libras, do governo federal): fica para depois da eleição. Carrega script e recursos de terceiros, então exige mudar a CSP e a Política de Privacidade, além de teste de desempenho no celular. Complementa o menu de acessibilidade (exceção C).
- **Transparência e trilha de auditoria da checagem** (commit `9b9343d`, guardado no branch `transparencia-pos-eleicao`; decisão do usuário na auditoria de 19/10: NÃO vai para produção antes de 25/10). É relevante para o projeto FAPERR (edital 002/2026). Problemas que as revisões acharam e que precisam ser resolvidos antes de publicar:
  - trilha **montada na hora** para checagens antigas, sem registro real: pode afirmar "Nenhuma checagem prévia localizada" quando a busca nas agências falhou (a falha não fica gravada no resultado);
  - etapas marcadas como **"sucesso" sem medição** (extração, IA);
  - **`except Exception` sem log** em `to_out`, que esconde dado corrompido e cai no fallback;
  - texto **"rastreabilidade completa"** (promete mais do que entrega);
  - **status só por cor** (o ícone está `aria-hidden`; o leitor de tela e o alto contraste não distinguem alerta de sucesso);
  - **plataformas abertas aparecem como "fonte"** (contraria a regra de não atribuir origem a quem só hospeda);
  - **"Link citado"** em "Plataformas consultadas", mas o sistema nunca acessa esses links;
  - **ruff vermelho** (14 erros e 2 arquivos sem formatação).
- **Identidade visual nova da página:** fica para depois de 25/10 (decisão de 19/10).
- A latência de fundo continua: a API está na Virgínia (Render) e o banco em São Paulo (Supabase), e cada transação custa ~0,7 s. Isso só muda colocando os dois na mesma região, avaliado depois da eleição.


## 15. Infraestrutura para o 2º turno (decisões do usuário, 15/10/2026)

Depois dos testes de carga (14/10, 30 usuários; 15/10, 150 usuários: 0% de erro, p95 0,93 s, nenhum 503):
- **Render continua no plano Free (custo zero).** O UptimeRobot mantém a instância acordada (`/health` a cada 5 min). Risco aceito: o Free dorme por inatividade (até ~50 s na 1ª requisição) e tem pouca CPU; o teste de 300 usuários mede o teto real nesse plano.
- **Uma instância só, sem autoscaling e sem `--workers`:** os caches em memória (apuração, mapa, `/checks/latest`, `/comments`, página da checagem) supõem um processo só (`docs/DEPLOY.md` §3.8).
- **Aprovado (15/10):** cache curto de `GET /checks/{id}` (página pública da checagem), com invalidação imediata a cada checagem nova e na exclusão de conta.
- **Teste de 300 usuários autorizado** depois do deploy desse cache: só GET, com acessos repetidos a uma mesma checagem (link viral).
