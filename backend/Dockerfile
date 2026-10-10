# Imagem do backend — Cloud Run (principal) e Render (plano B).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    APP_ENV=production \
    PORT=8080

WORKDIR /srv

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
# CA público do Supabase para validar o certificado do banco (DATABASE_SSL_ROOT_CERT).
COPY certs ./certs

RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8080

# Cloud Run e Render injetam $PORT. --proxy-headers: ambos ficam atrás de um proxy HTTPS
# (necessário para o esquema https correto). ATENÇÃO: com --forwarded-allow-ips='*',
# request.client.host vem do X-Forwarded-For e pode ser falsificado — nunca use para
# segurança/rate limit (os limites são por usuário autenticado).
# --timeout-graceful-shutdown 8: o Cloud Run dá 10s após o SIGTERM.
# --limit-concurrency: acima disso, 503 em vez de afogar a instância (Render Free: 0,1 CPU).
CMD ["sh", "-c", "exec uvicorn --factory app.main:create_app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*' --no-access-log --timeout-graceful-shutdown 8 --limit-concurrency ${UVICORN_LIMIT_CONCURRENCY:-100}"]
