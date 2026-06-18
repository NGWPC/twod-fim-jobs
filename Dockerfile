FROM ghcr.io/prefix-dev/pixi:0.70.2-bookworm AS builder

WORKDIR /app

COPY pyproject.toml pixi.lock load_env.sh ./
COPY twod_fim_jobs ./twod_fim_jobs

RUN pixi install --frozen --environment prod

FROM debian:trixie-slim AS prod

COPY --from=builder /app/.pixi/envs/prod /app/.pixi/envs/prod

ENV PATH="/app/.pixi/envs/prod/bin:$PATH"

ENTRYPOINT ["twod_fim_jobs"]
