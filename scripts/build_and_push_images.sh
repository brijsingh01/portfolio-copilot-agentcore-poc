#!/usr/bin/env bash
# Build the three linux/arm64 images locally and push them to the ECR repositories created by 02-artifacts.
#   usage: scripts/build_and_push_images.sh <env> <tag> [region]
#   then:  python -m cloudformation.deploy images --env <env> --image-tag <tag>
# Needs Docker with buildx; on x86 hosts arm64 emulation (QEMU) - Docker Desktop includes it.
set -euo pipefail
ENV_NAME=${1:?environment name, e.g. poc}
TAG=${2:?image tag, e.g. v1}
REGION=${3:-${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}}
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REGISTRY="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com"
PREFIX="portfolio-copilot-${ENV_NAME}"
cd "$(dirname "$0")/.."

aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$REGISTRY"

build() {  # <repository> <APP_MODULE> <APP_PORT>
  echo ">> ${REGISTRY}/${PREFIX}/$1:${TAG}"
  docker buildx build --platform linux/arm64 --provenance=false -f docker/Dockerfile \
    --build-arg "APP_MODULE=$2" --build-arg "APP_PORT=$3" --build-arg "SERVICE_NAME=$1" \
    -t "${REGISTRY}/${PREFIX}/$1:${TAG}" --push .
}

build quant-mcp      src.quant_mcp.app       8000
build market-analyst src.market_analyst.app  9000
build orchestrator   src.orchestrator.agent  8080
echo "pushed :${TAG} to ${REGISTRY}/${PREFIX}/{quant-mcp,market-analyst,orchestrator}"
