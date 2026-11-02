#!/usr/bin/env bash
# infra/register.sh <training-job-name>  -- register + approve; prints the package ARN
set -euo pipefail
source "$(dirname "$0")/env.sh"
JOB=$1
MODEL_DATA=$(aws sagemaker describe-training-job --training-job-name $JOB --query ModelArtifacts.S3ModelArtifacts --output text)
PKG=$(aws sagemaker create-model-package --model-package-group-name $PROJECT \
  --model-approval-status PendingManualApproval \
  --inference-specification "{
    \"Containers\":[{\"Image\":\"$IMAGE_URI\",\"ModelDataUrl\":\"$MODEL_DATA\",
      \"Environment\":{\"PROMPTMUSIC_TEXT_ENCODER\":\"/opt/ml/model/encoder\"}}],
    \"SupportedContentTypes\":[\"application/json\"],
    \"SupportedResponseMIMETypes\":[\"application/zip\"],
    \"SupportedRealtimeInferenceInstanceTypes\":[\"ml.g5.xlarge\"]}" \
  --model-metrics "{\"ModelQuality\":{\"Statistics\":{\"ContentType\":\"application/json\",
    \"S3Uri\":\"s3://$BUCKET/eval/$JOB/evaluation.json\"}}}" --query ModelPackageArn --output text)
aws sagemaker update-model-package --model-package-arn $PKG --model-approval-status Approved >/dev/null
echo $PKG
