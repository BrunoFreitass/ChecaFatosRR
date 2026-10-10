# ChecaFatosRR

Site apartidário contra a desinformação no 2º turno das eleições presidenciais de 2026, com foco em
Roraima. O usuário cola um link e recebe uma análise automatizada, com as checagens das agências.
O site também mostra a apuração oficial do TSE.

## Estrutura

```
backend/     API FastAPI (Python 3.12) — Cloud Run (Render como plano B)
frontend/    React + Vite + Tailwind — Cloudflare Pages
supabase/    migrations/ (esquema do banco) · manual/ (SQL aplicado à mão)
docs/        DEPLOY.md
```

- Plano e decisões: `.claude/plans/checafatosrr.plan.md`
- Deploy: [`docs/DEPLOY.md`](docs/DEPLOY.md)

## Rodar localmente

```bash
# Backend
cd backend && python -m venv .venv && .venv/Scripts/activate   # Linux/Mac: source .venv/bin/activate
pip install -r requirements-dev.txt && cp .env.example .env
uvicorn app.main:create_app --factory --reload --port 8000

# Frontend (outro terminal)
cd frontend && npm install && cp .env.example .env.local && npm run dev
```

## Testes

Os mesmos comandos da CI (`.github/workflows/ci.yml`):

```bash
cd backend && ruff check . && ruff format --check . && pytest -W error
cd frontend && npm run lint && npm test && npm run build && npm run e2e
```

- Testes de integração do backend (`tests/integration`) precisam de um Postgres **descartável**
  com `citext` (nunca o Supabase de produção: os testes criam usuários falsos e apagam
  `gemini_usage`). Na CI isso é automático. No Windows, sem Docker:
  1. baixe os *binaries* do PostgreSQL 17 em <https://www.enterprisedb.com/download-postgresql-binaries>
     e extraia (o pgAdmin não é necessário);
  2. `pgsql/bin/initdb -D dados -U postgres -A trust -E UTF8 --locale=C`;
  3. `pgsql/bin/pg_ctl -D dados -o "-p 55432 -c listen_addresses=localhost" -l pg.log start`;
  4. `pgsql/bin/createdb -h localhost -p 55432 -U postgres teste`;
  5. em `backend/`: `TEST_DATABASE_URL=postgresql://postgres@localhost:55432/teste pytest -W error`
     (PowerShell: `$env:TEST_DATABASE_URL="..."; pytest -W error`).
- `npm run e2e` (Playwright) sobe o Vite sozinho e simula a API. Na primeira vez:
  `npx playwright install chromium`.

## Andamento

| Etapa | Status |
|---|---|
| 0. Esqueleto, banco, Dockerfile, CI | ✅ |
| 1. Login Google + apelido | ✅ |
| 2. Checagem de links e de textos colados | 🧪 implementada, aguardando teste manual com as chaves reais |
| Deploy público | ⏳ |
| 5. Apuração TSE | ⏳ |
| 3+4. Comentários + moderação | ⏳ |
| 6. Anúncios · 7. Admin · 8. Páginas legais · 9. Deploy final | ⏳ |
