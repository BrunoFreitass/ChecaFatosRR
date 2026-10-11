# Deploy do ChecaFatosRR

Custo zero: Supabase (Free), **Render (Free)** para o backend, Cloudflare Pages (Free) para o
site. O Google Cloud Run é o plano B (seção 4): exige faturamento ativo no Google Cloud.
Os valores entre `<>` são seus; nunca faça commit de senhas ou chaves.

> **Limites do Render Free que derrubam o serviço:** 750 h de instância por mês **por workspace**
> (um serviço ligado 24 h gasta 744 h em outubro: não crie outro serviço Free) e a banda de saída
> incluída no workspace (confira o valor atual na página de preços). Sem forma de pagamento,
> estourar qualquer um **suspende o serviço até o fim do mês**. Acompanhe o uso no painel.

---

## 1. Supabase (banco)

1. Crie o projeto em <https://supabase.com/dashboard>. Use a região **South America (São Paulo)**.
   Guarde a senha do banco.
2. Aplique as migrações **na ordem do nome do arquivo**. Escolha **uma** das opções:
   - **SQL Editor**: cole e rode, um de cada vez:
     1. `supabase/migrations/20261008000000_init.sql` (etapas 0 e 1);
     2. `supabase/migrations/20261009000000_checagem.sql` (etapa 2: checagem de links e textos);
     3. `supabase/migrations/20261010000000_prompt_version.sql` (etapa 2: versão do prompt no cache);
     4. `supabase/migrations/20261010000001_checks_listed_link_idx.sql` (etapa 2: índice de "Últimas").
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
5. **Certificado do banco (obrigatório no deploy público):** baixe-o em *Project Settings →
   Database → SSL Configuration* e salve como **`backend/certs/supabase-ca.crt`** (é o certificado
   público da CA do Supabase; pode ir para o git). Ele entra na imagem pelo `Dockerfile`
   (`COPY certs ./certs`) e o deploy define `DATABASE_SSL_ROOT_CERT=/srv/certs/supabase-ca.crt`:
   a conexão valida a cadeia e o nome do servidor (verify-full). Local: no `backend/.env`, use
   `DATABASE_SSL_ROOT_CERT=certs/supabase-ca.crt`. Caminho errado ou arquivo que não é PEM faz o
   backend falhar já na inicialização, com mensagem clara.

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
uvicorn app.main:create_app --factory --reload --port 8000
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

## 3. Backend no Render (principal)

O `render.yaml` na raiz do repositório descreve o serviço (Blueprint): Docker com o mesmo
`backend/Dockerfile`, região **Virginia** (a mais próxima do Supabase em São Paulo), plano
**Free**, health check em `/health`, deploy automático **só depois da CI verde** e previews
desligados. Plano Free: 512 MB e **0,1 CPU**; dorme após 15 min sem acesso (por isso o
keep-alive da seção 3.4) e pode ser reiniciado pelo Render a qualquer momento.

### 3.1 Criar o serviço

1. <https://dashboard.render.com> → crie a conta (com **2FA**) e conecte o GitHub dando acesso
   **só a este repositório**. Não convide outros membros: quem tem acesso ao workspace vê as
   variáveis.
2. *New → Blueprint* → escolha o repositório. O Render lê o `render.yaml` e pede os valores das
   variáveis marcadas com `sync: false` (tabela abaixo).
3. Proteja a branch `main` no GitHub (merge só por PR com a CI verde): o `render.yaml` na `main`
   é a infraestrutura.

### 3.2 Variáveis

| Nome | Segredo? | Valor |
|---|---|---|
| `DATABASE_URL` | **Sim** | do `backend/.env`: Transaction pooler (porta 6543); senha com `@ / # : ?` em URL-encode |
| `SUPABASE_SECRET_KEY` | **Sim** | do `backend/.env` (`sb_secret_…`, já rotacionada) |
| `BAN_HASH_SALT` | **Sim** | do `backend/.env`, **o mesmo valor** (o banco é o mesmo; outro salt invalida banimentos) |
| `GEMINI_API_KEY` | **Sim** | do `backend/.env` (chave do projeto do AI Studio, nível gratuito) |
| `GOOGLE_FACTCHECK_API_KEY` | **Sim** | do `backend/.env` |
| `SUPABASE_JWT_SECRET` | **Sim** | **só se** não estiver vazia no `backend/.env` (projeto com "Legacy JWT Secret"); acrescente no painel |
| `SUPABASE_URL` | Não | `https://<project_ref>.supabase.co` |
| `ADMIN_EMAIL` | Sensível | seu e-mail de admin |
| `CORS_ORIGINS` | Não | `https://<seu-projeto>.pages.dev` (provisório: `http://localhost:5173`; tire o localhost depois) |
| `GEMINI_DAILY_CHECKS` / `GEMINI_DAILY_MODERATION` | Não | pelo RPD do modelo (a soma não passa da cota) |
| `APP_ENV`, `LOG_LEVEL`, `DATABASE_SSL_ROOT_CERT`, `GEMINI_MODEL_FALLBACKS`, `DB_POOL_SIZE`, `DB_POOL_MAX_OVERFLOW`, `EXTRACT_CONCURRENCY`, `UVICORN_LIMIT_CONCURRENCY` | Não | já definidas no `render.yaml` |

| `GEMINI_THINKING_LEVEL` | Não | **não defina** (padrão: thinking desligado por `thinking_budget=0`). Emergência sem deploy: `low` se o log `gemini_usage` mostrar `thoughts_tokens` > 0 (o 3.8-flash recusa `minimal`) |
| `PORT` | — | **não defina**: o Render injeta |

**Tempo de resposta:** o Render responde atrás do Cloudflare, que corta a requisição em ~100 s
(erro 524). Por isso a checagem cabe em ~90 s no pior caso (`REQUEST_LIMIT_SECONDS`): 8 s de folga para o banco +
download 20 s + agências 12 s + Gemini 48 s (uma tentativa de 25 s por modelo antes de trocar).

**Trocar a ordem dos modelos** (ex.: o 3.8 lento de forma consistente): a variável
`GEMINI_MODEL_FALLBACKS` tem valor fixo no `render.yaml`, então a mudança vai **no arquivo, por
PR** (a sincronização do Blueprint sobrescreveria um valor trocado só no painel). Para efeito
imediato, troque também no painel (*Environment*): o serviço reinicia com a nova ordem. A ordem
não entra no `PROMPT_VERSION`: o cache continua valendo.

As demais variáveis do `backend/app/config.py` usam o valor padrão. Copie cada valor do `backend/.env`
direto para o painel: nunca por chat, issue, print ou commit (`ADMIN_EMAIL` também é dado pessoal).

### 3.3 Conferir o deploy

- `https://<servico>.onrender.com/health` → `{"status":"ok"}`
- `/health/ready` → `{"status":"ok","database":"ok"}` (verify-full com o certificado do Supabase;
  resultado em cache de 60 s; uma falha fica 10 s). Com o pool lotado (pico de checagens), ele
  também pode dar 503: o site está de fato saturado naquele momento.
- `/docs`, `/redoc` e `/openapi.json` → 404
- Logs (*Dashboard → Logs*): JSON, sem segredos; filtre por `request_id` ou `"severity": "WARNING"`.

### 3.4 Keep-alive e monitoramento (UptimeRobot, grátis)

Crie a conta em <https://uptimerobot.com> com **2FA** e contatos de alerta por **e-mail e push**
(app do celular). Monitores:

| # | Tipo | Nome | URL | Intervalo | Detalhes |
|---|---|---|---|---|---|
| 1 | HTTP(s) | API /health | `https://<servico>.onrender.com/health` | **5 min** | método **GET** (não HEAD); timeout 30 s. Mantém o serviço acordado (dorme com 15 min sem acesso). |
| 2 | Keyword | API /health/ready | `https://<servico>.onrender.com/health/ready` | **60 min** | palavra-chave `"database":"ok"`, alertar quando **não** existir; método GET. Toca o banco e evita a pausa do Supabase Free (7 dias sem uso). |
| 3 | Keyword | Site | `https://<seu-projeto>.pages.dev/` | 5 min | palavra-chave `ChecaFatosRR`; opcional (avisa se o Pages cair). |

Teste os alertas uma vez (suspenda o serviço no Render por 10 min e veja o aviso chegar). No dia
25/10, a reserva se o UptimeRobot falhar é abrir o `/health` manualmente a cada 10 min.

### 3.5 Contingência para 25/10

**Sem cartão no Render** (o que já protege o serviço, sem custo):
- cache de 24 h por link/texto e "Últimas" com um item por link: leituras repetidas não chamam o
  Gemini;
- sem cota do Gemini, o resultado da versão anterior do prompt é servido com o aviso "Análise
  anterior"; sem nenhuma análise, a resposta é "esgotada hoje" (o site continua no ar);
- limites por usuário (10 checagens novas/h, 20/dia, 60 consultas/h) e cota diária do Gemini;
- apuração do TSE (etapa 5) por **cache único no banco** com single-flight: muitos acessos, uma
  busca no TSE a cada 30 s; o loop em segundo plano fica desligado no Free;
- **prompt congelado** e nenhum deploy de 22 a 26/10 (cada deploy reinicia o serviço);
- acompanhar diariamente, de 20/10 em diante, as horas e a banda no painel do Render. Sem cartão
  não há como evitar a suspensão se um limite estourar: o risco fica aceito.

**Com cartão** (cadastrar **até 20/10**; prefira cartão virtual com limite baixo):
1. Ative os alertas de gasto do Render e teste uma vez a troca Free → Starter → Free fora do pico.
2. **24/10 à noite**, pelo **painel** (não por commit no `render.yaml`): plano **Starter**
   (0,5 CPU, 512 MB, sempre ligado; ~US$ 7/mês, confira a cobrança proporcional na página de
   preços). Com forma de pagamento, banda excedente vira cobrança em vez de suspensão (o teto é o
   limite do cartão virtual).
3. **26/10**: volte para o Free e **remova o cartão**.

### 3.6 Domínio próprio (opcional: `checafatosrr.com.br`)

Com domínio, a API fica atrás do proxy grátis do Cloudflare (cache e limite por IP). A proteção da
banda do Render só vale **depois** do header secreto de origem (abaixo): sem ele, quem chamar
`<servico>.onrender.com` direto passa por fora do Cloudflare. No Cloudflare (DNS do domínio):

| Nome | Tipo | Destino | Proxy |
|---|---|---|---|
| `checafatosrr.com.br` (raiz) | domínio personalizado do **Pages** (*Pages → Custom domains*) | projeto do Pages | ligado |
| `www` | CNAME → `checafatosrr.com.br` + regra de redirecionamento 301 para a raiz | — | ligado |
| `api` | CNAME → `<servico>.onrender.com` | — | **desligado** até o Render validar o domínio; depois **ligado** |

Passos: no Render, *Settings → Custom Domains → `api.checafatosrr.com.br`*; espere o certificado;
ligue o proxy no Cloudflare com SSL/TLS em **Full (strict)** (nunca "Flexible", que deixa o trecho
Cloudflare → Render sem criptografia; erro 525/526 = certificado do Render ainda não emitido). Depois atualize `CORS_ORIGINS`
(`https://checafatosrr.com.br`), `VITE_API_URL` (`https://api.checafatosrr.com.br`, refaça o
deploy do Pages para a CSP acompanhar), Site URL e Redirect URLs do Supabase e as origens do
OAuth do Google. Regras do Cloudflare a criar (grátis):
- **cache de 30 s** só para `GET` em `/checks/latest` e `/checks/<public_id>`, só respostas **200**,
  e **bypass** quando a requisição tiver o header `Authorization` (o `POST /checks` nunca entra);
  o backend manda `no-store`, então a regra precisa sobrepor o TTL apenas nesses caminhos;
- **limite de taxa por IP** no `POST /checks`. O header secreto que impede o acesso direto ao
`*.onrender.com` ainda não está implementado: entra junto com o domínio.

---

## 4. Plano B — Google Cloud Run

> Só use se o Render não servir: o Cloud Run exige faturamento ativo no Google Cloud (alerta
> de orçamento obrigatório).
>
> **Dois projetos do Google Cloud.** O projeto criado pelo AI Studio (`gen-lang-client-…`) guarda
> **só a chave do Gemini** e fica **sem faturamento**: com faturamento vinculado, a chave sai do
> nível gratuito e passa a ser cobrada por uso. O OAuth do Google, a Fact Check, o Cloud Run, o
> Cloud Build, o Artifact Registry e o Secret Manager ficam no **outro projeto** (aqui,
> `checafatosrr`), com faturamento e alerta de orçamento; a chave do Gemini entra nele só como
> segredo. Nos comandos abaixo, `<PROJECT_ID>` é sempre o projeto do Cloud Run.

Pré-requisito: [gcloud CLI](https://cloud.google.com/sdk/docs/install) instalado. **Não precisa de
Docker local**: o `--source` envia o código e o Cloud Build monta a imagem com o `backend/Dockerfile`.

```bash
gcloud auth login
gcloud config set project <PROJECT_ID>
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com secretmanager.googleapis.com
```

### 4.1 Conta de serviço e segredos

**Conta de serviço dedicada** para o Cloud Run, sem nenhum papel no projeto: se o backend um
dia for comprometido, o token dela só lê os segredos do app (a conta padrão do Compute costuma
ser *Editor* do projeto inteiro).

```bash
gcloud iam service-accounts create checafatosrr-run --display-name "ChecaFatosRR (Cloud Run)"
```

**Segredos pelo console** (*Security → Secret Manager → Create secret*), um por vez, colando o
valor no campo do navegador: assim o valor nunca passa pelo terminal nem fica no histórico do
shell. Cinco segredos: `DATABASE_URL`, `SUPABASE_SECRET_KEY`, `BAN_HASH_SALT`, `GEMINI_API_KEY` e
`GOOGLE_FACTCHECK_API_KEY` (use os valores atuais do seu `backend/.env`, com as chaves já
rotacionadas). As outras variáveis não são secretas e vão em `--set-env-vars`.

Depois, só a conta dedicada lê cada segredo:

```bash
for SECRET in DATABASE_URL SUPABASE_SECRET_KEY BAN_HASH_SALT GEMINI_API_KEY GOOGLE_FACTCHECK_API_KEY; do
  gcloud secrets add-iam-policy-binding "$SECRET" \
    --member="serviceAccount:checafatosrr-run@<PROJECT_ID>.iam.gserviceaccount.com" \
    --role="roles/secretmanager.secretAccessor"
done
```

Para trocar um valor depois: no console, *Secret Manager → segredo → New version*; e faça um novo
deploy (ou `gcloud run services update ... --update-secrets ...`).

### 4.2 Deploy

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
  --service-account checafatosrr-run@<PROJECT_ID>.iam.gserviceaccount.com \
  --cpu 1 --memory 512Mi --concurrency 20 --timeout 180 \
  --set-env-vars "^@^APP_ENV=production@LOG_LEVEL=INFO@CORS_ORIGINS=https://<seu-projeto>.pages.dev@SUPABASE_URL=https://<project_ref>.supabase.co@ADMIN_EMAIL=<seu-email>@DATABASE_SSL_ROOT_CERT=/srv/certs/supabase-ca.crt@GEMINI_MODEL_FALLBACKS=gemini-3.8-flash,gemini-3.7-flash@GEMINI_DAILY_CHECKS=<pelo RPD>@GEMINI_DAILY_MODERATION=<pelo RPD>" \
  --set-secrets "DATABASE_URL=DATABASE_URL:latest,SUPABASE_SECRET_KEY=SUPABASE_SECRET_KEY:latest,BAN_HASH_SALT=BAN_HASH_SALT:latest,GEMINI_API_KEY=GEMINI_API_KEY:latest,GOOGLE_FACTCHECK_API_KEY=GOOGLE_FACTCHECK_API_KEY:latest"
```

> `--set-env-vars "^@^..."`: o `^@^` troca o separador das variáveis para `@`, porque
> `GEMINI_MODEL_FALLBACKS` tem vírgula no valor.
>
> `--timeout 180`: uma checagem nova leva até ~80 s no pior caso (download 20 s + agências
> 12 s + Gemini 48 s, já contando novas tentativas e a troca de modelo). Se outra pessoa
> estiver checando o mesmo conteúdo, a requisição espera pelo resultado dela e, se a outra
> falhar, assume a checagem só nos primeiros 2 s (no máximo ~90 s, com o banco).

> **Projeto com "Legacy JWT Secret" (passo 5 da seção 1.1)?** Crie também o segredo
> `SUPABASE_JWT_SECRET` (como na seção 3.1) e acrescente
> `,SUPABASE_JWT_SECRET=SUPABASE_JWT_SECRET:latest` ao `--set-secrets`. Sem isso, todo login
> dá 401 em produção.

Teste: `curl https://<url-do-servico>/health` e `curl https://<url-do-servico>/health/ready`.

Na primeira execução o gcloud pergunta se pode criar o repositório `cloud-run-source-deploy` no
Artifact Registry: responda **Y**.

**Se o build falhar com "permission denied"** (comum em projetos criados recentemente), dê à conta
de serviço padrão do Compute (que o Cloud Build usa para montar a imagem) as permissões de build.
Confira em *IAM* se essa conta tem o papel **Editor**: se tiver, troque-o por estes quatro (o app
em si roda com a `checafatosrr-run`, que não tem nenhum papel no projeto):

```bash
PROJECT_NUMBER=$(gcloud projects describe <PROJECT_ID> --format='value(projectNumber)')
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

### 4.3 Dia da eleição — 25/10/2026

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

### 4.4 Logs

```bash
gcloud run services logs read checafatosrr-api --region southamerica-east1 --limit 100
```

Os logs são JSON. No Cloud Logging dá para filtrar por `jsonPayload.request_id` ou `severity>=WARNING`.

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
3. **Variáveis de ambiente** (Production **e** Preview; ou desligue os deploys de preview em
   *Settings → Builds → Branch deployments*, porque o build falha sem as variáveis):
   `VITE_API_URL=https://<servico>.onrender.com`,
   `VITE_SUPABASE_URL`, `VITE_SUPABASE_PUBLISHABLE_KEY` e
   **sem** `NODE_VERSION`: a versão vem do `frontend/.node-version` (a mesma da CI). Se a variável
   existir, ela vence o arquivo; apague-a. O `frontend/.npmrc` (`engine-strict`) faz o build falhar
   se o Node não atender aos pacotes, em vez de só avisar (EBADENGINE).
4. Cada push na `main` publica sozinho.

Faça o deploy **sempre pela integração com o Git**. Um `wrangler pages deploy` de um `dist`
montado na sua máquina usaria as variáveis locais (API em localhost). No Cloudflare
(`CF_PAGES=1`) o build recusa localhost e chave `sb_secret_` no lugar da publicável.

Rotas da SPA: sem um `404.html`, o Pages serve o `index.html` para qualquer caminho. Por isso
`/qualquer/rota` funciona sem configuração. Os cabeçalhos de segurança ficam em `frontend/public/_headers`.

**CSP:** o build (`npm run build`) acrescenta a `Content-Security-Policy` ao `dist/_headers`, montada
a partir de `VITE_API_URL` e `VITE_SUPABASE_URL` (`frontend/csp.ts`). Sem `unsafe-inline` nem
`unsafe-eval`; `connect-src` só libera o próprio site, a API e o Supabase. O build **falha** se
as variáveis faltarem ou se a API não for `https`. Os e2e (`npm run e2e`) rodam no build de
produção com a mesma CSP e falham em qualquer violação. Mudou a URL da API ou do Supabase? Refaça
o deploy do Pages para a CSP acompanhar.

---

## 6. Checklist pós-deploy

- [ ] `GET /health` e `GET /health/ready` respondem 200 no Render.
- [ ] `CORS_ORIGINS` contém exatamente a URL do Pages (sem `/` no final).
- [ ] O site abre no celular e a página "não encontrada" funciona em uma rota inventada.
- [ ] Monitores do UptimeRobot ativos (seção 3.4) e só **um** serviço Free no workspace do Render.
- [ ] Data API do Supabase desligada para `public`.
- [ ] `curl -sI https://<seu-projeto>.pages.dev | grep -i content-security-policy` mostra a CSP
      com a URL do Render e a do Supabase.
- [ ] `/health/ready` responde `database: ok` com `DATABASE_SSL_ROOT_CERT` ligado (verify-full).
