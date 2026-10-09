# Deploy do ChecaFatosRR

Custo zero: Supabase (Free), Google Cloud Run (free tier), Cloudflare Pages (Free).
Os valores entre `<>` são seus; nunca faça commit de senhas ou chaves.

> **Proteja o bolso**: no Google Cloud, crie um alerta de orçamento
> (Faturamento → Orçamentos e alertas → R$ 10/mês). O Cloud Run exige conta de faturamento
> ativa mesmo dentro da camada gratuita.

---

## 1. Supabase (banco)

1. Crie o projeto em <https://supabase.com/dashboard>. Use a região **South America (São Paulo)**.
   Guarde a senha do banco.
2. Aplique as migrações **na ordem do nome do arquivo**. Escolha **uma** das opções:
   - **SQL Editor**: cole e rode, um de cada vez:
     1. `supabase/migrations/20261008000000_init.sql` (etapas 0 e 1);
     2. `supabase/migrations/20261009000000_checagem.sql` (etapa 2: checagem de links e textos).
   - **CLI**:
     ```bash
     npx supabase init          # só na primeira vez (cria supabase/config.toml)
     npx supabase link --project-ref <project_ref>
     npx supabase db push
     ```
3. **Desligue a Data API** para o schema `public`: *Project Settings → Data API*, tire `public`
   de "Exposed schemas". O frontend usa o Supabase só para o login, que continua funcionando.
4. Pegue a string de conexão: *Connect → Connection string → **Transaction pooler*** (porta **6543**).
   Ela vira o `DATABASE_URL` do backend. Se a senha tiver `@ / # : ?`, use URL-encode (`@` → `%40`).
5. (Recomendado até a etapa 9) baixe o certificado em *Database Settings → SSL Configuration*.
   Com `DATABASE_SSL_ROOT_CERT` apontando para ele, o backend valida o certificado do banco.
   Sem isso a conexão é criptografada, mas o certificado não é validado.

> O papel dedicado `app_backend` (privilégio mínimo) fica para a etapa 9:
> `supabase/manual/app_backend_role.sql`. Até lá use o usuário padrão do pooler.

---

## 1.1 Login com Google (OAuth)

**No Google Cloud Console** (<https://console.cloud.google.com>):

1. **Tela de consentimento**: *APIs e serviços → Tela de consentimento OAuth* (ou *Google Auth
   Platform → Branding*).
   - Nome do app: ChecaFatosRR; e-mail de suporte: o seu.
   - Público: **Externo**.
   - Escopos: só `openid`, `email` e `profile`.
   - Domínio autorizado: `supabase.co`.
   - Enquanto testa, cadastre o seu e-mail em **Usuários de teste**.
2. **Cliente**: *Credenciais → Criar credenciais → ID do cliente OAuth → Aplicativo da Web*.
   - Origens JavaScript autorizadas: `http://localhost:5173`. O domínio do Pages entra no deploy.
   - URI de redirecionamento autorizado: `https://<project_ref>.supabase.co/auth/v1/callback`
   - Copie o **Client ID** e o **Client secret**.

**No Supabase:**

3. *Authentication → Sign In / Providers → Google*: ative e cole o Client ID e o Client secret.
   **Desative o provedor Email**, para que o login seja só com Google.
4. *Authentication → URL Configuration*:
   - **Site URL**: `http://localhost:5173` (depois, a URL do Pages).
   - **Redirect URLs**: `http://localhost:5173/auth/callback` (depois, `https://<seu-projeto>.pages.dev/auth/callback`).
5. *Project Settings → JWT Keys*:
   - Se houver **JWT Signing Keys (ECC/RSA)**, deixe `SUPABASE_JWT_SECRET` vazio.
   - Se só houver o **Legacy JWT Secret**, copie o valor para `SUPABASE_JWT_SECRET`.

**Antes do deploy público:**
- No Google, mude o app para **Em produção**. Sem isso, só os usuários de teste conseguem entrar.
  Isso exige a URL da Política de Privacidade (`https://<seu-projeto>.pages.dev/privacidade`).
- Adicione a origem e o redirect do Pages nos dois painéis (Google e Supabase).
- Preencha o e-mail de contato em `frontend/src/legal.ts`.

## 1.2 Checagem: Gemini e Fact Check Tools (etapa 2)

1. **Gemini** — <https://aistudio.google.com> → *Get API key* → *Create API key*.
   - Copie para `GEMINI_API_KEY` (segredo).
   - Em *Rate limits*, veja a **cota diária (RPD)** do modelo Flash e ajuste
     `GEMINI_DAILY_CHECKS` e `GEMINI_DAILY_MODERATION` para caber nela (a soma das duas não
     deve passar da cota). O padrão é 200 + 200.
   - `GEMINI_MODEL_FALLBACKS=gemini-3.8-flash,gemini-3.7-flash`: cadeia de modelos com nome
     fixo (nunca `latest`/`preview`). Se a cota diária do primeiro acabar, a checagem usa o
     próximo; o log `gemini_model_responded` mostra qual respondeu. `GEMINI_MODEL` não é mais
     usado: apague-o do `backend/.env` se ainda estiver lá.
2. **Fact Check Tools API** — no mesmo projeto do GCP:
   - *APIs e serviços → Biblioteca* → ative **Fact Check Tools API**;
   - *Credenciais → Criar credenciais → Chave de API*;
   - edite a chave: em *Restrições de API*, deixe **só** a Fact Check Tools API;
   - copie para `GOOGLE_FACTCHECK_API_KEY` (segredo). Sem ela, a checagem funciona, mas sem as
     checagens das agências.
3. Teste local: com o backend rodando, entre no site, cole um link de notícia e um texto.
   O primeiro resultado demora de 10 a 30 segundos; o mesmo conteúdo de novo vem do cache.

---

## 2. Backend local

```bash
cd backend
python -m venv .venv
# Windows: .venv\Scripts\activate    ·    Linux/Mac: source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env               # preencha DATABASE_URL
uvicorn app.main:app --reload --port 8000
```

- <http://localhost:8000/health> → `{"status":"ok"}`
- <http://localhost:8000/health/ready> → confirma a conexão com o banco
- <http://localhost:8000/docs> → documentação da API (desligada em produção)

Testes e lint:

```bash
pytest
ruff check . && ruff format --check .
```

---

## 3. Backend no Google Cloud Run

Pré-requisito: [gcloud CLI](https://cloud.google.com/sdk/docs/install) instalado. **Não precisa de
Docker local**: o `--source` envia o código e o Cloud Build monta a imagem com o `backend/Dockerfile`.

```bash
gcloud auth login
gcloud config set project <PROJECT_ID>
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com secretmanager.googleapis.com
```

### 3.1 Segredos (Secret Manager)

Cinco segredos: `DATABASE_URL`, `SUPABASE_SECRET_KEY`, `BAN_HASH_SALT`, `GEMINI_API_KEY` e
`GOOGLE_FACTCHECK_API_KEY`. As outras variáveis não são secretas e vão em `--set-env-vars`.

```bash
printf '%s' '<DATABASE_URL do pooler>'   | gcloud secrets create DATABASE_URL --data-file=-
printf '%s' '<sb_secret_...>'            | gcloud secrets create SUPABASE_SECRET_KEY --data-file=-
printf '%s' '<o mesmo BAN_HASH_SALT do .env>' | gcloud secrets create BAN_HASH_SALT --data-file=-
printf '%s' '<chave do AI Studio>'       | gcloud secrets create GEMINI_API_KEY --data-file=-
printf '%s' '<chave da Fact Check API>'  | gcloud secrets create GOOGLE_FACTCHECK_API_KEY --data-file=-

# Permite que o Cloud Run leia os segredos (conta de serviço padrão do Compute):
PROJECT_NUMBER=$(gcloud projects describe <PROJECT_ID> --format='value(projectNumber)')
for SECRET in DATABASE_URL SUPABASE_SECRET_KEY BAN_HASH_SALT GEMINI_API_KEY GOOGLE_FACTCHECK_API_KEY; do
  gcloud secrets add-iam-policy-binding "$SECRET" \
    --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
    --role="roles/secretmanager.secretAccessor"
done
```

Para trocar o valor depois: `printf '%s' '<novo>' | gcloud secrets versions add DATABASE_URL --data-file=-`
e faça um novo deploy.

### 3.2 Deploy

**Ordem na primeira vez** (o CORS do backend precisa da URL do Pages, e o Pages precisa da URL
do backend):

1. Faça o deploy do backend com `CORS_ORIGINS=http://localhost:5173` (provisório).
2. Faça o deploy do frontend (seção 5) com `VITE_API_URL` = URL do Cloud Run.
3. Atualize só o CORS, sem rebuild:
   `gcloud run services update checafatosrr-api --region southamerica-east1 --update-env-vars "CORS_ORIGINS=https://<seu-projeto>.pages.dev"`

O `.gcloudignore` em `backend/` impede que o `.env` e o `.venv` sejam enviados ao Cloud Build.

Rode na raiz do repositório:

```bash
gcloud run deploy checafatosrr-api \
  --source backend \
  --region southamerica-east1 \
  --allow-unauthenticated \
  --min-instances 0 --max-instances 3 \
  --cpu 1 --memory 512Mi --concurrency 40 --timeout 180 \
  --set-env-vars "^@^APP_ENV=production@LOG_LEVEL=INFO@CORS_ORIGINS=https://<seu-projeto>.pages.dev@SUPABASE_URL=https://<project_ref>.supabase.co@ADMIN_EMAIL=<seu-email>@GEMINI_MODEL_FALLBACKS=gemini-3.8-flash,gemini-3.7-flash@GEMINI_DAILY_CHECKS=200@GEMINI_DAILY_MODERATION=200" \
  --set-secrets "DATABASE_URL=DATABASE_URL:latest,SUPABASE_SECRET_KEY=SUPABASE_SECRET_KEY:latest,BAN_HASH_SALT=BAN_HASH_SALT:latest,GEMINI_API_KEY=GEMINI_API_KEY:latest,GOOGLE_FACTCHECK_API_KEY=GOOGLE_FACTCHECK_API_KEY:latest"
```

> `--set-env-vars "^@^..."`: o `^@^` troca o separador das variáveis para `@`, porque
> `GEMINI_MODEL_FALLBACKS` tem vírgula no valor.
>
> `--timeout 180`: uma checagem nova leva até ~92 s no pior caso (download 20 s + agências
> 12 s + Gemini 60 s, já contando novas tentativas e a troca de modelo). Se outra pessoa
> estiver checando o mesmo conteúdo, a requisição espera pelo resultado dela e, se a outra
> falhar, assume a checagem em até 80 s (no máximo ~172 s).

> **Projeto com "Legacy JWT Secret" (passo 5 da seção 1.1)?** Crie também o segredo
> `SUPABASE_JWT_SECRET` (como na seção 3.1) e acrescente
> `,SUPABASE_JWT_SECRET=SUPABASE_JWT_SECRET:latest` ao `--set-secrets`. Sem isso, todo login
> dá 401 em produção.

Teste: `curl https://<url-do-servico>/health` e `curl https://<url-do-servico>/health/ready`.

Na primeira execução o gcloud pergunta se pode criar o repositório `cloud-run-source-deploy` no
Artifact Registry: responda **Y**.

**Se o build falhar com "permission denied"** (comum em projetos criados recentemente), dê à conta
de serviço padrão as permissões de build:

```bash
for ROLE in roles/cloudbuild.builds.builder roles/run.builder roles/artifactregistry.writer roles/logging.logWriter; do
  gcloud projects add-iam-policy-binding <PROJECT_ID> \
    --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" --role="$ROLE"
done
```

Se o `--allow-unauthenticated` falhar por política da organização (contas Google Workspace),
crie o projeto fora da organização ou peça ao administrador para liberar `allUsers` no Cloud Run.

> O gcloud separa as variáveis por vírgula. Por isso, para ter **mais de uma origem** em
> `CORS_ORIGINS`, troque o separador do gcloud por `@`:
> `--set-env-vars "^@^APP_ENV=production@LOG_LEVEL=INFO@CORS_ORIGINS=https://a.pages.dev,https://b.com.br"`.

### 3.3 Dia da eleição — 25/10/2026

As urnas fecham às 17h (Brasília), 16h em Roraima. Para evitar a espera do *cold start* durante a
apuração, mantenha **1 instância sempre ligada** (isso tem um custo pequeno, de centavos a poucos
reais por dia):

```bash
# 25/10, por volta das 15h (Brasília):
gcloud run services update checafatosrr-api --region southamerica-east1 --min-instances 1

# Depois da divulgação do resultado final, volte ao custo zero:
gcloud run services update checafatosrr-api --region southamerica-east1 --min-instances 0
```

Para conferir: `gcloud run services describe checafatosrr-api --region southamerica-east1 --format=yaml | grep -i minScale`.

### 3.4 Logs

```bash
gcloud run services logs read checafatosrr-api --region southamerica-east1 --limit 100
```

Os logs são JSON. No Cloud Logging dá para filtrar por `jsonPayload.request_id` ou `severity>=WARNING`.

---

## 4. Plano B — Render

Use se o Cloud Run der problema. O Dockerfile é o mesmo.

1. <https://dashboard.render.com> → *New → Web Service* → conecte o repositório.
2. **Root Directory**: `backend` · **Runtime**: Docker · **Instance**: Free.
3. **Health Check Path**: `/health`.
4. Variáveis: as mesmas do Cloud Run (`APP_ENV`, `CORS_ORIGINS`, `DATABASE_URL`, …).
5. Atualize o `VITE_API_URL` do frontend para a URL do Render e refaça o deploy do frontend.

> O plano Free do Render "dorme" após 15 min sem acesso. O primeiro acesso depois disso leva
> cerca de 1 minuto.

---

## 5. Frontend no Cloudflare Pages

Local:

```bash
cd frontend
npm install
cp .env.example .env.local
npm run dev                         # http://localhost:5173
npm run build                       # gera dist/
```

Deploy pela integração com o Git (recomendado):

1. Cloudflare → *Workers & Pages → Create → Pages → Connect to Git*.
2. **Root directory**: `frontend` · **Build command**: `npm run build` · **Output**: `dist`.
3. **Variáveis de ambiente** (Production): `VITE_API_URL=https://<url-do-cloud-run>`,
   `VITE_SUPABASE_URL`, `VITE_SUPABASE_PUBLISHABLE_KEY` e
   `NODE_VERSION=24` (o Vite 8 exige Node recente; o arquivo `frontend/.node-version` também fixa a versão).
4. Cada push na `main` publica sozinho.

Alternativa sem Git: `npx wrangler pages deploy dist --project-name checafatosrr`.

Rotas da SPA: sem um `404.html`, o Pages serve o `index.html` para qualquer caminho. Por isso
`/qualquer/rota` funciona sem configuração. Os cabeçalhos de segurança ficam em `frontend/public/_headers`.

---

## 6. Checklist pós-deploy

- [ ] `GET /health` e `GET /health/ready` respondem 200 no Cloud Run.
- [ ] `CORS_ORIGINS` contém exatamente a URL do Pages (sem `/` no final).
- [ ] O site abre no celular e a página "não encontrada" funciona em uma rota inventada.
- [ ] Alerta de orçamento criado no Google Cloud.
- [ ] Data API do Supabase desligada para `public`.
