FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Системные пакеты не нужны: psycopg[binary] везёт свой libpq, healthcheck — на Python.
ARG REQUIREMENTS=requirements/base.txt
COPY requirements/ requirements/
RUN pip install -r ${REQUIREMENTS}

COPY . .

# Каталоги томов создаются заранее, чтобы том унаследовал владельца app.
RUN useradd --create-home --uid 1000 app && mkdir -p /app/media /app/beat \
    && chown -R app:app /app
USER app

COPY --chown=app:app docker/entrypoint.sh /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3"]
