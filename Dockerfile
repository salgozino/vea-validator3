#
# Builder image
#

# uv version pinned for reproducible installs
ARG UV_VERSION=0.11.27
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

# Python version for vea-challenger (matches .python-version)
FROM python:3.12-alpine AS builder

COPY --from=uv /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependency layer, cached independently of source changes
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,id=uv-cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# Source code copy step
COPY src ./src
COPY README.md ./

# Install the project itself into the venv built above
RUN --mount=type=cache,id=uv-cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


# ----------------------------------------------------------------------------------------------------
# ----------------------------------------------------------------------------------------------------
# ----------------------------------------------------------------------------------------------------


#
# Deployable image
#
# This is the final layer that will be deployed and run

FROM python:3.12-alpine AS vea-challenger

# Fixed uid/gid (not -S/system range) so a host-side `chown 1000:1000` on a
# bind-mounted ./data works the same on every host.
RUN addgroup -g 1000 app && adduser -D -u 1000 -G app app \
    && mkdir -p /data && chown -R app:app /data

USER app
WORKDIR /app

# Copy only the built venv and source from the builder (same absolute path as
# the builder stage: the venv's shebangs are baked with this path at build time)
COPY --from=builder --chown=app:app /app/.venv ./.venv
COPY --from=builder --chown=app:app /app/src ./src

ENV PATH="/app/.venv/bin:${PATH}" \
    VEA_DB_PATH=/data/vea-challenger.db

ENTRYPOINT ["vea-challenger"]
CMD ["run"]
