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
uvicorn app.main:app --reload --port 8000

# Frontend (outro terminal)
cd frontend && npm install && cp .env.example .env.local && npm run dev
```

## Testes

Os mesmos comandos da CI (`.github/workflows/ci.yml`):

```bash
cd backend && ruff check . && ruff format --check . && pytest -W error
cd frontend && npm run lint && npm run build
```

## Andamento

| Etapa | Status |
|---|---|
| 0. Esqueleto, banco, Dockerfile, CI | ✅ |
| 1. Login Google + apelido | ⏳ |
| 2. Checagem de links | ⏳ |
| Deploy público | ⏳ |
| 5. Apuração TSE | ⏳ |
| 3+4. Comentários + moderação | ⏳ |
| 6. Anúncios · 7. Admin · 8. Páginas legais · 9. Deploy final | ⏳ |
