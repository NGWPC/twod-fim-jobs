FROM ghcr.io/prefix-dev/pixi:0.70.2-bookworm AS builder

WORKDIR /app

COPY pyproject.toml pixi.lock load_env.sh ./
COPY twod_fim_jobs ./twod_fim_jobs

RUN pixi install --frozen --environment prod

FROM debian:trixie-slim AS two-dim-fim-base

COPY --from=builder /app/.pixi/envs/prod /app/.pixi/envs/prod

ENV PATH="/app/.pixi/envs/prod/bin:$PATH"

ENTRYPOINT ["twod_fim_jobs"]

FROM two-dim-fim-base AS health

ENTRYPOINT ["twod_fim_jobs", "health"]

FROM two-dim-fim-base AS build_model

ENTRYPOINT ["twod_fim_jobs", "build_model"]

FROM two-dim-fim-base AS modify_network

ENTRYPOINT ["twod_fim_jobs", "modify_network"]

FROM deltares/sfincs-cpu:sfincs-v2.4.0-Galibier-Release AS run_kwse_scenarios-sfincs

COPY --from=builder /app/.pixi/envs/prod /app/.pixi/envs/prod

ENV PATH="/app/.pixi/envs/prod/bin:$PATH"

ENTRYPOINT ["twod_fim_jobs", "run_kwse_scenarios"]

# FROM 172866912423.dkr.ecr.us-east-1.amazonaws.com/lisflood-fp:sha-6a82d50f56ced47af7fb9dbb1db37b98368e8159-cpu AS two-dim-fim-lisflood

# COPY --from=builder /app/.pixi/envs/prod /app/.pixi/envs/prod

# ENV PATH="/app/.pixi/envs/prod/bin:$PATH"

# ENTRYPOINT ["twod_fim_jobs"]
