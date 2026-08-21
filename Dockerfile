# ============================================================================
# Builder Stage
# ============================================================================
# Make a Python environment with pixi
FROM ghcr.io/prefix-dev/pixi:0.70.2-bookworm AS builder

WORKDIR /app

COPY pyproject.toml pixi.lock load_env.sh ./
COPY twod_fim_jobs ./twod_fim_jobs

RUN pixi install --frozen --environment prod


# ============================================================================
# Base Stage
# ============================================================================
# Minimal base image with python environment, used to access any non-solver job
FROM debian:trixie-slim AS two-dim-fim-base

COPY --from=builder /app/.pixi/envs/prod /app/.pixi/envs/prod

ENV PATH="/app/.pixi/envs/prod/bin:$PATH"

ENTRYPOINT ["twod_fim_jobs"]


# ============================================================================
# Job-Specific Stages
# ============================================================================

# Health check service
FROM two-dim-fim-base AS health

ENTRYPOINT ["twod_fim_jobs", "health"]


# Build model stage
FROM two-dim-fim-base AS build_model

ENTRYPOINT ["twod_fim_jobs", "build_model"]


# KWSE scenarios with SFINCS solver (not yet implemented)
FROM deltares/sfincs-cpu:sfincs-v2.4.0-Galibier-Release AS run_kwse_scenarios-sfincs

COPY --from=builder /app/.pixi/envs/prod /app/.pixi/envs/prod

ENV PATH="/app/.pixi/envs/prod/bin:$PATH"

ENTRYPOINT ["twod_fim_jobs", "run_kwse_scenarios"]


# ND scenarios with LISFLOOD-FP solver
FROM ghcr.io/dewberry/lisflood-fp:sha-aa006ae776b084eac5d00c8b165d2f1e1f689b0d-gpu AS run_nd_scenarios-lisflood

COPY --from=builder /app/.pixi/envs/prod /app/.pixi/envs/prod

ENV PATH="/app/.pixi/envs/prod/bin:$PATH"

ENTRYPOINT ["twod_fim_jobs", "run_nd_scenarios"]


# ============================================================================
# Development Stage
# ============================================================================
# LISFLOOD-FP solver with pixi for dev work (private image)
FROM ghcr.io/dewberry/lisflood-fp:sha-aa006ae776b084eac5d00c8b165d2f1e1f689b0d-gpu AS two-dim-fim-lisflood-dev

COPY --from=ghcr.io/prefix-dev/pixi:0.70.2-bookworm /usr/local/bin/pixi /usr/local/bin/pixi

# Create non-root user matching the host user to avoid root-owned files on bind mounts
ARG USER_UID=1000
ARG USER_GID=1000
RUN groupadd --gid ${USER_GID} vscode \
    && useradd --uid ${USER_UID} --gid ${USER_GID} -m --shell /bin/bash vscode \
    && mkdir -p /workspaces/twod-fim-jobs \
    && chown vscode:vscode /workspaces/twod-fim-jobs

WORKDIR /workspaces/twod-fim-jobs
USER vscode

# Install dev env on first start (source is bind-mounted from WSL at runtime)
CMD ["bash", "-c", "pixi install --frozen && tail -f /dev/null"]
