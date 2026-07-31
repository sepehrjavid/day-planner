# syntax=docker/dockerfile:1

# Base images are pinned to an exact patch version rather than :3.12-slim, so a
# rebuild six months from now produces the same interpreter.
#
# For full supply-chain reproducibility, pin by digest instead — a tag can be
# repointed, a digest cannot:
#   docker pull python:3.12.8-slim-bookworm
#   docker inspect --format='{{index .RepoDigests 0}}' python:3.12.8-slim-bookworm
# then replace the FROM lines with python@sha256:<digest>.
ARG PYTHON_IMAGE=python:3.12.8-slim-bookworm

# ---------------------------------------------------------------------------
# Build stage — resolve dependencies into a self-contained virtualenv.
# Kept separate so pip, its cache, and any transient build tooling never reach
# the runtime image.
# ---------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# ---------------------------------------------------------------------------
# Runtime stage
# ---------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PATH="/opt/venv/bin:$PATH" \
    PORT=8080

# Pick up any base-image security patches published since the tag was cut,
# then create an unprivileged account to run as.
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app \
      --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin app

# Everything stays root-owned while the process runs as uid 10001: the app can
# read its own code and dependencies but cannot rewrite them, so a foothold in
# the process can't persist itself into the filesystem. This also means the
# container runs fine with a read-only root filesystem.
COPY --from=builder --chown=root:root /opt/venv /opt/venv

WORKDIR /srv
COPY --chown=root:root app ./app

# Normalise modes rather than inheriting the build host's umask: dirs need the
# traverse bit or uid 10001 cannot even see inside them, and files want to be
# world-readable but not writable. Doing this explicitly keeps the image
# identical whether it was built on a developer laptop or in CI.
RUN find /srv/app -type d -exec chmod 0755 {} + \
 && find /srv/app -type f -exec chmod 0644 {} +

USER 10001:10001

EXPOSE 8080

# Shell form so Cloud Run's injected $PORT is honoured; `exec` keeps uvicorn as
# PID 1 so SIGTERM reaches it directly and shutdown isn't a 10s timeout.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
