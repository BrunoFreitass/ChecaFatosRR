# Plano: Etapa 1 — Login com Google + apelido

**Plano-mãe**: `.claude/plans/checafatosrr.plan.md` (seções 3, 4 e 9)
**Complexidade**: Média · **Estimativa**: 1 a 1,5 dia

## Resumo
Login **só com Google** via Supabase Auth (fluxo PKCE). O backend valida o JWT do Supabase
(JWKS; HS256 como fallback). No primeiro acesso, o usuário escolhe um apelido único e aceita os
Termos e a Política de Privacidade. O apelido passa pelo mesmo filtro de conteúdo dos comentários,
cujo núcleo é criado agora com testes obrigatórios. Contas banidas (`banned_identities`) não
conseguem criar perfil. Inclui também "Excluir minha conta" (LGPD).

## Padrões a seguir (da etapa 0)
| Categoria | Origem | Padrão |
|---|---|---|
| Erros | `backend/app/errors.py:27` | `AppError(status, code, mensagem_pt_br, extra, headers)` → `{"error": {...}}` |
| Config | `backend/app/config.py:61` | campos em `Settings` + `get_settings()` com cache; validação no próprio campo |
| Banco | `backend/app/db.py:58` | `get_session()`; escritas dentro de `async with session.begin()` |
| Logs | `backend/app/middleware.py` | `logger.info("evento", extra={...})`, nunca com dado pessoal (sem e-mail ou token) |
| Testes | `backend/tests/conftest.py:13` | fixtures `app`/`client` com env isolado; testes de fumaça em `tests/smoke`, unitários em `tests/unit` |
| Frontend | `frontend/src/router.tsx` | rotas filhas do `Layout`; `<title>` por página; `env.ts` para as variáveis |

**Desvio do plano-mãe**: em vez de `models.py` com ORM, uso **repositórios com SQL explícito**
(`sqlalchemy.text`), um por agregado. Motivo: as regras de concorrência revisadas são SQL puro
(`on conflict`, `for update`, CTEs), e o repositório é trocado por um fake nos testes, sem
precisar de banco.

## Fluxo
1. **Entrar**: `supabase.auth.signInWithOAuth({ provider: "google", options: { redirectTo: <origem>/auth/callback } })`.
2. **`/auth/callback`**: o supabase-js troca o `code` pela sessão (PKCE) e chama `GET /me`.
   - **200**: volta para a página de onde o usuário veio.
   - **404 `profile_not_found`**: vai para `/bem-vindo` (onboarding).
3. **Onboarding**:
   - O campo de apelido consulta `GET /apelido/disponivel?q=` com debounce de 400 ms.
   - O checkbox obrigatório tem links para Termos e Privacidade.
   - O envio chama `POST /me`.
4. **Cabeçalho**: botão "Entrar" ou menu com apelido, "Minha conta" e "Sair".
5. **Minha conta**: apelido, status (ativo, suspenso até X, banido) e "Excluir minha conta" (com confirmação).

## Backend

### Verificação do token (`app/auth.py`)
- **Biblioteca**: PyJWT com `PyJWKClient`, apontando para `{SUPABASE_URL}/auth/v1/.well-known/jwks.json`. As chaves ficam em cache e são recarregadas sozinhas quando a chave de assinatura muda.
- **Algoritmos aceitos**: só **ES256/RS256**. HS256 só vale se `SUPABASE_JWT_SECRET` estiver definido. `none` é sempre recusado.
- **Checagens do token**: `aud == "authenticated"`, `iss == {SUPABASE_URL}/auth/v1`, `exp` (tolerância de 30 s), `sub` precisa ser UUID e `role == "authenticated"`.
- **`CurrentUser`**:
  - `id` (`sub`), `email` (minúsculo) e `email_verified`;
  - `provider` (`app_metadata.provider`);
  - `google_sub`, lido de `user_metadata.provider_id` ou, na falta dele, de `user_metadata.sub`.
- **Admin**: `email == ADMIN_EMAIL` e `email_verified` e `provider == "google"`.
- **Dependências**: `current_user` (401 com `WWW-Authenticate: Bearer`), `current_profile` (404 `profile_not_found` se não houver perfil) e `require_admin` (403).

### Endpoints

| Método e rota | Auth | Resposta |
|---|---|---|
| `GET /me` | logado | `{apelido, status, suspended_until, is_admin, terms_version, created_at}` ou 404 `profile_not_found` |
| `POST /me` `{apelido, aceito_termos: true, terms_version}` | logado | 201 com o perfil |
| `GET /apelido/disponivel?q=` | logado | `{disponivel, motivo?}` com `motivo ∈ {formato, reservado, conteudo_proibido, em_uso}` |
| `DELETE /me` | logado | 204 |

**`POST /me` pode responder:**
- 422 `validation_error`: formato inválido.
- 422 `apelido_proibido`: aviso de violação explicando a regra. Fica registrado em `moderation_log` (`nickname_blocked`). Não gera strike, porque o usuário ainda não tem perfil.
- 409 `apelido_em_uso`: violação de unicidade (`23505`).
- 409 `perfil_existente`.
- 422 `termos_desatualizados`: a `terms_version` é diferente de `CURRENT_TERMS_VERSION`.
- 403 `conta_banida`: o `HMAC(BAN_HASH_SALT, google_sub)` está em `banned_identities`.

**`DELETE /me`:**
- Chama a Admin API do Supabase (`DELETE {SUPABASE_URL}/auth/v1/admin/users/{id}`) com `SUPABASE_SECRET_KEY`.
- A cascata apaga o perfil, e o trigger da migração limpa os textos do log de moderação.

### Filtro de conteúdo — núcleo (`app/services/moderation/local_filter.py`) [TESTE OBRIGATÓRIO]
- **Normalização**:
  - minúsculas e sem acentos (NFKD);
  - leetspeak: `0→o 1→i 3→e 4→a 5→s 7→t @→a $→s`;
  - letras repetidas colapsadas (`porrrra → pora`, testando as duas formas);
  - separadores removidos (`p.o.r.r.a`, `p o r r a`).
- **Duas listas** (`wordlist_pt.txt`, seções `[forte]` e `[curta]`):
  - termos **fortes** bloqueiam como substring do apelido;
  - termos **curtos** ou ambíguos (ex.: "cu") só bloqueiam como token inteiro, para não barrar "cuca" ou "computador".
- **Saída**: `FilterResult(blocked: bool, category: "palavrao" | "sexual" | None, matched_rule: str)`. A etapa 4 aumenta a lista e reaproveita o resultado nos comentários.
- **Nomes reservados**: admin, moderador, checafatos, tse, suporte, oficial, entre outros.

### Banimento (`app/services/bans.py`) [TESTE OBRIGATÓRIO]
`ban_hash(google_sub) = HMAC-SHA256(BAN_HASH_SALT, google_sub)`, com 32 bytes. A etapa 4/7 grava o hash ao banir; aqui só fazemos a consulta.

### Dependências novas
`PyJWT[crypto]` e `httpx`, na versão estável atual, ambas fixadas.

## Frontend
- **`@supabase/supabase-js`**: `lib/supabase.ts` com `flowType: "pkce"`, `persistSession` e `autoRefreshToken`.
- **`lib/api.ts`**: `fetch` com `Authorization: Bearer <access_token>`. Converte `{error}` em `ApiError(code, message, extra)`.
- **`auth/AuthProvider.tsx`**: guarda sessão e perfil, reage a `onAuthStateChange` e expõe `useAuth()`.
- **Componentes**: `RequireAuth` / `RequireProfile` (protegem rotas) e `UserMenu`.
- **Páginas**: `Entrar`, `AuthCallback`, `Onboarding` (`/bem-vindo`) e `MinhaConta`.
- **Termos e Privacidade agora, como rascunho**: `legal/Termos` e `legal/Privacidade` entram nesta etapa marcados como "versão preliminar". Motivo: o deploy público vem logo depois da etapa 2, e o Google exige a URL da Política de Privacidade para tirar o app do modo de teste. A versão final continua na etapa 8.

## Testes
- **Obrigatórios (unitários)**:
  - `local_filter`: variantes `p0rr4`, `p.o.r.r.a`, acentos, repetição, e falsos positivos ("cuca", "computador", "Scunthorpe").
  - Validação de apelido.
  - `ban_hash`.
  - Verificação do JWT, com um par de chaves ES256 gerado no teste: válido, expirado, `aud` errado, `iss` errado, `alg: none`, HS256 sem segredo configurado, assinatura inválida.
  - Regra de admin.
- **De fumaça**: `/me`, `POST /me` e `/apelido/disponivel`, com o repositório trocado por um fake em memória (dependency override), cobrindo 401, 404, 201, 409, 422 e 403.
- **Manual**, no seu projeto Supabase:
  - login real;
  - onboarding;
  - apelido duplicado em duas abas;
  - sair e entrar;
  - excluir a conta e conferir no Table Editor que o perfil sumiu.
- **E2E (Playwright)**: começa na etapa 2. O login do Google não é automatizável; os testes vão usar uma sessão injetada.

## Arquivos
| Arquivo | Ação |
|---|---|
| `backend/app/auth.py`, `app/schemas/profiles.py`, `app/repositories/profiles.py`, `app/routers/profiles.py` | CRIAR |
| `backend/app/services/moderation/{__init__,local_filter}.py`, `wordlist_pt.txt`, `app/services/bans.py`, `app/legal.py` (`CURRENT_TERMS_VERSION`) | CRIAR |
| `backend/app/config.py`, `app/main.py`, `requirements*.txt`, `.env.example` | ALTERAR |
| `backend/tests/unit/test_local_filter.py`, `test_nickname.py`, `test_bans.py`, `test_auth.py`; `tests/smoke/test_profiles.py` | CRIAR |
| `frontend/src/lib/{supabase,api}.ts`, `src/auth/*`, `src/components/UserMenu.tsx`, `src/pages/{Entrar,AuthCallback,Onboarding,MinhaConta}.tsx`, `src/pages/legal/{Termos,Privacidade}.tsx` | CRIAR |
| `frontend/src/router.tsx`, `components/Layout.tsx`, `vite-env.d.ts`, `.env.example`, `package.json` | ALTERAR |
| `docs/DEPLOY.md` (seção Google OAuth), `README.md` | ALTERAR |

## Validação
```bash
cd backend && ruff check . && ruff format --check . && pytest -W error
cd frontend && npm run lint && npm run build
```
Depois: `/python-review` + typescript-reviewer + security-reviewer (o login é superfície de ataque) → `/code-review` → teste manual → checagem de segredos → commit.

## Riscos
| Risco | Prob. | Mitigação |
|---|---|---|
| App OAuth em "modo de teste" no Google: só usuários de teste entram | ALTA | Publicar o app ("In production") antes do deploy público. Escopos básicos (email, profile, openid) não exigem verificação, mas pedem a URL da Política de Privacidade (rascunho nesta etapa). |
| Projeto Supabase com chave JWT legada (HS256) | MÉDIA | O backend aceita as duas; a etapa 9 recomenda migrar para chaves assimétricas. |
| Formato da Admin API com as chaves novas (`sb_secret_…`) | MÉDIA | Testar `DELETE /me` no seu projeto; o fallback é a `service_role` legada. |
| Sessão no localStorage (padrão do supabase-js), exposta a XSS | MÉDIA | Sem `dangerouslySetInnerHTML`; CSP restrita na etapa 9; token de curta duração. |
| `user_metadata.provider_id` ausente no JWT | BAIXA | Fallback para `user_metadata.sub`; teste com o seu token real no teste manual. |
| Falso positivo do filtro barrando apelido legítimo | MÉDIA | Duas listas (forte/curta) e testes de falso positivo; a mensagem diz qual regra foi violada. |

## Ajustes aprovados (08/10/2026)
1. **Testes de integração com Postgres real, só na CI** (service container `postgres` do GitHub Actions), marcados `@pytest.mark.integration` e pulados sem `TEST_DATABASE_URL`. Um `bootstrap.sql` simula o que o Supabase já tem (`auth.users`, `auth.identities`, schema `extensions`, papéis `anon`/`authenticated`/`service_role`) e depois aplica a migração real. Cobrem:
   - apelido duplicado (`23505`) → 409 `apelido_em_uso`;
   - trigger de resposta em 1 nível;
   - bloqueio de conta banida.
2. **O hash de banimento usa o `sub` da identidade Google lido de `auth.identities.provider_id`** (`provider = 'google'`), consultado no banco pelo backend. Nunca usa o `user_metadata` do JWT: o usuário pode alterar esse campo com `supabase.auth.updateUser` e assim escapar do banimento.
   - Risco na etapa 9: o papel `app_backend` vai precisar de `select (user_id, provider, provider_id)` em `auth.identities`. Se o Supabase não permitir, o fallback é a Admin API (`GET /auth/v1/admin/users/{id}` → `identities[].identity_data.sub`).

## Resultado da revisão (08/10/2026)
**Códigos de erro reais de `POST /me`:**
- `apelido_invalido` (422, formato), `apelido_reservado` (422), `apelido_proibido` (422, com `regra` e `explicacao`);
- `apelido_em_uso` (409), `perfil_existente` (409);
- `termos_desatualizados` (422, com `terms_version`);
- `conta_banida` (403), `login_google_obrigatorio` (403);
- `unauthorized` (401, quando o usuário já foi apagado).

**Corrigido após as revisões (python, typescript, security, code-reviewer):**
- **Filtro:** escapes com dígitos (`puta1`) e com letras repetidas (`cuuuzao`); palavras ambíguas (`rola`, `pica`, `corno`) proibidas só em apelido; reservados com leet e prefixo (`adm1n`, `AdminRR`).
- **JWKS:** busca forçada limitada (anti-DoS por `kid` inventado).
- **Banco:** `hide_parameters` nos logs de SQL; erro 23503 vira 401.
- **Admin API:** nova tentativa só com `apikey` quando há 401.
- **CI:** falha, em vez de pular, quando falta `TEST_DATABASE_URL`.
- **Frontend:** recarga sem desmontar a tela; checagem de disponibilidade que falha não trava o envio; sair separado de excluir; foco na confirmação; erro do OAuth também via hash.

**Adiado (registrado):**
- **Etapa 2:** rate limit em `POST /me` e `GET /apelido/disponivel` (abuso de `moderation_log`), dentro do módulo de rate limit.
- **Etapa 4:** quem está suspenso pode apagar a conta e voltar sem a suspensão (os strikes zeram). Avaliar gravar o hash também na suspensão.
- **Etapas 3/4:** checar `profiles.status` em toda escrita; o JWT de um banido vale até expirar.
- **Etapa 9:**
  - CSP restrita antes de os comentários entrarem em produção (o token fica no localStorage);
  - grant de `auth.identities` para o `app_backend`;
  - avaliar pool pequeno no lugar de NullPool (cada método do repositório abre uma conexão).
- **Desempenho:** o pacote JS tem 161 kB gzip por causa do supabase-js; dividir os chunks quando houver mais páginas.
