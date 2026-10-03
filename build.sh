#!/usr/bin/env bash
# Run the tests, then build and push the image.
#   ./build.sh [tag]
# Env: IMAGE (default ghcr.io/opswhisperer/kube-drift), PLATFORMS (default linux/amd64,linux/arm64).
set -euo pipefail
cd "$(dirname "$0")"

IMAGE=${IMAGE:-ghcr.io/opswhisperer/kube-drift}
PLATFORMS=${PLATFORMS:-linux/amd64,linux/arm64}
TAG=${1:-$(git describe --tags --always --dirty 2>/dev/null || date +%Y%m%d)}

python3 -m unittest discover -s tests -p 'test_*.py'

docker buildx build \
  --platform "$PLATFORMS" \
  --push \
  -t "${IMAGE}:${TAG}" \
  -t "${IMAGE}:latest" \
  .

echo "Pushed ${IMAGE}:${TAG} (and :latest) for ${PLATFORMS}"
