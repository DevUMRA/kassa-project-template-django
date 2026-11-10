# syntax=docker/dockerfile:1
# Qoldanba шаблонының Docker бейнесі. Талаптар: TEMPLATE.md («Docker бейнесі»).

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8000 \
    # Контейнерде waitress барлық интерфейсте тыңдайды; оған тек reverse proxy (Traefik) жете алады
    WAITRESS_HOST=0.0.0.0 \
    WAITRESS_TRUSTED_PROXY=* \
    BACKUP_DIR=/app/backups

# curl — healthcheck үшін міндетті: Coolify және docker compose оны контейнер ішінде іске қосады
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Тәуелділіктер бөлек қабатта: код өзгергенде қайта орнатылмайды
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Статиканы бейне жинағанда бір рет жинаймыз (whitenoise береді).
# Бұл кілттер тек collectstatic үшін — нақты мәндер іске қосылғанда ортадан келеді.
RUN DJANGO_SECRET_KEY=collectstatic-only DB_PASSWORD=collectstatic-only DJANGO_ALLOWED_HOSTS=localhost \
    python manage.py collectstatic --noinput \
    && chmod +x docker-entrypoint.sh

# root емес пайдаланушы
RUN useradd --system --uid 10001 --no-create-home app \
    && mkdir -p /app/backups && chown 10001 /app/backups
USER 10001

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/api/health/" || exit 1

ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["sh", "-c", "WAITRESS_PORT=${PORT} exec python serve.py"]
