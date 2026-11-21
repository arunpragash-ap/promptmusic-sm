#!/usr/bin/env bash
# infra/up.sh  -- bring the endpoint back from what cleanup-keep-model.sh kept
#   INSTANCE_TYPES="ml.c6i.2xlarge" infra/up.sh   to choose the machine types (default: GPU first, CPU last)
set -euo pipefail
cd "$(dirname "$0")/.."
source infra/env.sh
[ -z "${PKG:-}" ] && [ -f .pkg ] && source .pkg
if [ -z "${PKG:-}" ]; then
  PKG=$(aws sagemaker list-model-packages --model-package-group-name $PROJECT \
    --model-approval-status Approved --sort-by CreationTime --sort-order Descending \
    --query "ModelPackageSummaryList[0].ModelPackageArn" --output text)
  echo "export PKG=$PKG" > .pkg
fi
echo "deploying $PKG"
infra/deploy.sh "$PKG"
infra/autoscale.sh
