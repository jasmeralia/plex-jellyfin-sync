FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

ARG SYNC_UID=568
ARG SYNC_GID=568

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml /app/pyproject.toml
COPY plex_jellyfin_sync /app/plex_jellyfin_sync

RUN rm -f /app/plex_jellyfin_sync/codex_loop.py /app/plex_jellyfin_sync/responses_loop.py \
    && python -m pip install --no-cache-dir .

RUN groupadd --gid "${SYNC_GID}" sync \
    && useradd --uid "${SYNC_UID}" --gid "${SYNC_GID}" --create-home --home-dir /home/sync sync \
    && mkdir -p /config /state /home/sync \
    && chown -R sync:sync /config /state /home/sync

USER sync

ENTRYPOINT ["tini", "--"]
CMD ["python", "-m", "plex_jellyfin_sync", "--config", "/config/config.yaml"]
