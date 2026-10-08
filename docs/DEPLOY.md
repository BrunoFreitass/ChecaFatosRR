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
2. Aplique a migração. Escolha **uma** das opções:
   - **SQL Editor**: cole o conteúdo de `supabase/migrations/20261008000000_init.sql` e clique em *Run*.
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

```bash
printf '%s' '<DATABASE_URL do pooler>' | gcloud secrets create DATABASE_URL --data-file=-

# Permite que o Cloud Run leia o segredo (conta de serviço padrão do Compute):
PROJECT_NUMBER=$(gcloud projects describe <PROJECT_ID> --format='value(projectNumber)')
gcloud secrets add-iam-policy-binding DATABASE_URL \
  --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"
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
  --cpu 1 --memory 512Mi --concurrency 40 --timeout 60 \
  --set-env-vars "APP_ENV=production,LOG_LEVEL=INFO,CORS_ORIGINS=https://<seu-projeto>.pages.dev" \
  --set-secrets "DATABASE_URL=DATABASE_URL:latest"
```

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
3. **Variáveis de ambiente** (Production): `VITE_API_URL=https://<url-do-cloud-run>` e
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
