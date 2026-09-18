#!/usr/bin/env bash
set -euo pipefail

build_model_image="twod-fim-jobs:e2e-build-model"
run_nd_image="twod-fim-jobs:e2e-run-nd"
run_kwse_image="twod-fim-jobs:e2e-run-kwse"
solver_mode="${1:-cpu}"

case "${solver_mode,,}" in
  gpu)
    solver_suffix="gpu"
    use_gpu="true"
    ;;
  cpu)
    solver_suffix="cpu"
    use_gpu="false"
    ;;
  *)
    echo "Usage: $0 [cpu|gpu]" >&2
    exit 1
    ;;
esac

docker build --target build_model \
  --tag "$build_model_image" .
docker build --target "run_nd_scenarios-lisflood-${solver_suffix}" \
  --tag "$run_nd_image" .
docker build --target "run_kwse_scenarios-lisflood-${solver_suffix}" \
  --tag "$run_kwse_image" .

docker image inspect \
  "$build_model_image" \
  "$run_nd_image" \
  "$run_kwse_image" >/dev/null

E2E_BUILD_MODEL_IMAGE="$build_model_image" \
E2E_ND_IMAGE="$run_nd_image" \
E2E_KWSE_IMAGE="$run_kwse_image" \
E2E_USE_GPU="$use_gpu" \
python -m pytest -m e2e tests/end_to_end/test_end_to_end_docker.py -v -s