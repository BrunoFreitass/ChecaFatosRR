# Plano: Deploy público (Cloud Run + Cloudflare Pages)

**Data:** 10/10/2026 · **Base:** `main` @ `1bf1444` (etapa 2 encerrada) · **Prazo:** 2º turno em 25/10/2026
**Complexidade:** Média. Pouco código; o grosso é configuração nos painéis e a ordem certa.
**Status:** AGUARDANDO APROVAÇÃO. Nada será publicado antes do "ok".

## Objetivo
Publicar o ChecaFatosRR em produção:
- backend FastAPI no **Cloud Run** (`southamerica-east1`, segredos no Secret Manager);
- frontend no **Cloudflare Pages** (`<projeto>.pages.dev`);
- o **Supabase** existente (Auth Google + Postgres via pooler, migrações já aplicadas).

Antes de publicar: `/security-scan`, revisão de segurança da configuração de deploy e a suíte completa.

## Quem faz o quê
| Tarefa | Quem | Por quê |
|---|---|---|
| Ajustes no repositório (fase 0) | Claude | código e docs |
| Valores dos segredos (`gcloud secrets create`) | **Usuário** | Claude não lê `.env` real (regra fixa) e não deve ver chaves |
| Painéis (Google Cloud Console, Supabase, Cloudflare, OAuth) | **Usuário**, com o passo a passo de `docs/DEPLOY.md` | conta e cobrança são do usuário |
| Comandos `gcloud run deploy` / `services update` | Usuário, ou Claude com autorização explícita em cada um | publicação é ação externa |
| Verificação pós-deploy (curl, logs) | Claude | leitura |

## Padrões existentes que o deploy segue
| Tema | Onde | Padrão |
|---|---|---|
| Imagem | `backend/Dockerfile` | `python:3.12-slim`, usuário sem root (uid 10001), `uvicorn --factory ... --proxy-headers`, porta `$PORT` |
| Upload | `backend/.gcloudignore` | não envia `.env`, `.venv` nem `.git` ao Cloud Build |
| Config | `backend/app/config.py` | `pydantic-settings`; segredos com `repr=False`; `APP_ENV=production` desliga `/docs` e `/openapi.json` (`app/main.py:34-36`) |
| Saúde | `app/routers/health.py` | `/health` (processo) e `/health/ready` (banco) |
| Frontend | `frontend/src/env.ts` | `VITE_API_URL`, `VITE_SUPABASE_URL`, `VITE_SUPABASE_PUBLISHABLE_KEY` obrigatórias no build |
| Cabeçalhos | `frontend/public/_headers` | nosniff, `X-Frame-Options DENY`, HSTS, COOP; CSP adiada para a etapa 9 |
| Variável com vírgula | `docs/DEPLOY.md` §3.2 | `--set-env-vars "^@^..."` |

## Fase 0: preparar o repositório (Claude, numa branch `deploy`, com PR e CI)
1. **Corrigir o comando local**: `uvicorn app.main:app` não sobe, porque o `main.py` só tem `create_app()`. Usar `uvicorn app.main:create_app --factory --reload --port 8000` no `README.md` e em `docs/DEPLOY.md` §2.
2. **`docs/DEPLOY.md` §1**: a lista de migrações vai para 4 arquivos (`20261008000000_init`, `20261009000000_checagem`, `20261010000000_prompt_version`, `20261010000001_checks_listed_link_idx`).
3. **CSP básica no `_headers`** (a decidir: pergunta 1). Valor: `default-src 'self'; connect-src 'self' <API> https://<ref>.supabase.co; img-src 'self' data:; style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self' https://<ref>.supabase.co`. A conferir no build de preview do Pages, antes da produção.
4. **Certificado do banco** (a decidir: pergunta 2). Colocar o CA público do Supabase na imagem (`backend/certs/`; é público, não é segredo) e `DATABASE_SSL_ROOT_CERT` → verify-full. Sem isso a conexão é criptografada, mas o certificado não é validado.
5. **`/security-scan` (AgentShield)** sobre agentes, hooks, MCP, permissões e segredos do repositório. Depois, o agente **security-reviewer** sobre a superfície do deploy:
   - Dockerfile e `.gcloudignore`;
   - CORS de produção e `--proxy-headers`;
   - cabeçalhos;
   - variáveis `VITE_*` (só valores públicos);
   - `/docs` desligado;
   - rotas públicas sem login (`/checks/{id}`, `/checks/latest`, `/health*`);
   - logs sem segredo.
6. **verification-loop**: suíte completa com integração no Postgres local, frontend completo, `/quality-gate`, `/code-review` e detect-secrets.
7. Commit, PR `deploy` → CI verde → merge na `main`. Os achados de segurança são corrigidos ou levados ao usuário antes do merge.

## Fase 1: Google Cloud (usuário, com os comandos prontos de `docs/DEPLOY.md` §3)
1. **Alerta de orçamento** (R$ 10/mês) **antes** de tudo.
2. Ativar as APIs: run, cloudbuild, artifactregistry, secretmanager.
3. Criar os 5 segredos: `DATABASE_URL`, `SUPABASE_SECRET_KEY`, `BAN_HASH_SALT`, `GEMINI_API_KEY`, `GOOGLE_FACTCHECK_API_KEY`. Se o projeto usar o "Legacy JWT Secret", também `SUPABASE_JWT_SECRET`. **Usar as chaves rotacionadas.**
4. Permissão `secretAccessor` para a conta de serviço do Cloud Run.

## Fase 2: backend no Cloud Run
1. `gcloud run deploy checafatosrr-api --source backend ...` (§3.2), com `CORS_ORIGINS=http://localhost:5173` provisório.
   - `GEMINI_DAILY_CHECKS` = valor do RPD real informado pelo usuário (pendência aberta).
   - `--max-instances 3 --timeout 180 --concurrency 40 --memory 512Mi`.
2. Verificar:
   - `/health` → 200;
   - `/health/ready` → `database: ok`;
   - `/docs` → 404;
   - `/checks/latest` → 200 sem login;
   - logs JSON sem segredos.

## Fase 3: frontend no Cloudflare Pages (usuário cria o projeto; Claude confere o build)
1. Connect to Git: root `frontend`, build `npm run build`, output `dist`, `NODE_VERSION=24`, branch de produção `main`.
2. Variáveis: `VITE_API_URL` = URL do Cloud Run, `VITE_SUPABASE_URL`, `VITE_SUPABASE_PUBLISHABLE_KEY`.
3. O primeiro build gera `https://<projeto>.pages.dev`.

## Fase 4: amarrar as URLs
1. **CORS**: `gcloud run services update ... --update-env-vars "CORS_ORIGINS=https://<projeto>.pages.dev"`, sem `/` no final.
2. **Supabase → Authentication → URL Configuration**:
   - Site URL = `https://<projeto>.pages.dev`;
   - Redirect URLs com `https://<projeto>.pages.dev/auth/callback`;
   - manter o localhost para desenvolvimento.
3. **Google OAuth**: origem `https://<projeto>.pages.dev`. Publicar o app como **Em produção**, com a URL de privacidade `/privacidade`. Só escopos `openid`, `email` e `profile`, então não precisa da verificação demorada do Google.
4. **Supabase**: confirmar a Data API desligada para `public`.

## Fase 5: teste de produção (usuário, com Claude acompanhando os logs)
1. No celular e no computador:
   - abrir o site;
   - rota inventada → "não encontrada";
   - login com Google → callback → escolher apelido.
2. Uma checagem de link e uma de texto colado. Usam a cota real do Gemini: é o teste do usuário, fora da regra das 3 chamadas do Claude.
3. Abrir o link público de uma checagem numa aba anônima; conferir "Últimas" e o compartilhamento no WhatsApp.
4. Checklist de `docs/DEPLOY.md` §6.

## Rollback
- **Backend**: `gcloud run services update-traffic checafatosrr-api --region southamerica-east1 --to-revisions <revisão-anterior>=100`.
- **Frontend**: Cloudflare Pages → Deployments → *Rollback* para o deploy anterior.
- **Banco**: as migrações da etapa 2 são aditivas; não há rollback de schema neste deploy.

## Riscos
| Risco | Prob. | Mitigação |
|---|---|---|
| CORS errado (barra no final, http) | Média | Teste da fase 5 logo depois da fase 4; comando de correção pronto, sem rebuild |
| Login quebrado em produção (Site URL, redirect, app OAuth em "teste") | Média | Fase 4 completa antes de divulgar; o diagnóstico do `/auth/callback` já mostra o motivo |
| `SUPABASE_JWT_SECRET` faltando em projeto "legacy" → todo login 401 | Baixa | Conferir o tipo de chave na fase 1 (DEPLOY §3.2, nota) |
| Cota do Gemini (RPD) estourar com tráfego real | Média | Ajustar `GEMINI_DAILY_CHECKS`; fallback de modelos; "análise anterior" sem cota; cache de 24 h |
| Limitador de apelido em memória vale por instância (até 3) | Baixa | Aceito no MVP: os limites de checagem ficam no banco |
| Supabase Free pausa o projeto após ~7 dias sem acesso | Baixa (com tráfego) | Monitorar; o tráfego da eleição mantém o projeto ativo |
| Cold start (`min-instances 0`) | Certa, leve | Aceito; no dia 25/10, `--min-instances 1` (DEPLOY §3.3) |
| CSP bloquear algo legítimo (se aprovada) | Média | Testar no build de preview do Pages antes de promover |
| Custo inesperado | Baixa | Alerta de orçamento na fase 1; `--max-instances 3` |

## Perguntas para o usuário
1. **CSP agora** (fase 0, item 3) ou só na etapa 9? Recomendo agora: o site é público, em período eleitoral, e a CSP é testada no preview.
2. **Certificado do banco (verify-full)** agora? Recomendo agora: é pequeno e fecha uma lacuna de segurança antes de abrir ao público.
3. **Domínio**: `<projeto>.pages.dev` basta, ou haverá domínio próprio? Domínio próprio muda CORS, OAuth e Supabase; dá para fazer depois.
4. **Quem roda o `gcloud run deploy`**: você, ou eu com autorização em cada comando? Os segredos ficam sempre com você.
5. **RPD real do Gemini**, para fixar `GEMINI_DAILY_CHECKS` no deploy.

## Aceite
- [ ] Fase 0 mergeada com CI verde; `/security-scan` e security-reviewer sem achados graves abertos
- [ ] `/health/ready` 200 em produção; `/docs` 404
- [ ] Login com Google, checagem de link e de texto funcionando em produção
- [ ] Checklist §6 do DEPLOY completo; alerta de orçamento ativo
