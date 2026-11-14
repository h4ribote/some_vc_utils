# syntax=docker/dockerfile:1.7
FROM python:3.12-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Debian trixie's PostgreSQL 17 client matches the postgres:17 server, so pg_dump and pg_restore can read its dumps.
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-17 \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 10001 svu \
    && useradd --system --uid 10001 --gid svu --home-dir /var/lib/svu --shell /usr/sbin/nologin svu \
    && install -d -o svu -g svu -m 750 /var/lib/svu /var/lib/svu/backups

WORKDIR /app

# Runtime dependencies are installed from pyproject.toml and constraints.txt alone so that code changes do not invalidate this layer.
COPY pyproject.toml constraints.txt ./
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']))" > /tmp/requirements.txt \
    && pip install --no-cache-dir -r /tmp/requirements.txt -c constraints.txt \
    && rm /tmp/requirements.txt

COPY svu ./svu
RUN pip install --no-cache-dir --no-deps -c constraints.txt . \
    && rm -rf build *.egg-info

USER svu
ENTRYPOINT ["svu"]
CMD ["--help"]
