#!/usr/bin/env bash
# infra/deploy.sh <model-package-arn>  -- create or update the async endpoint
set -euo pipefail
source "$(dirname "$0")/env.sh"
PKG=$1; EP=${EP:-${PROJECT}-async}
aws sagemaker create-model --model-name pm-model-$STAMP --execution-role-arn $ROLE_ARN \
  --primary-container ModelPackageName=$PKG >/dev/null
# InferenceAmiVersion: the default hosting AMI has driver 470 (CUDA 11.4); torch 2.6 needs 12.x
aws sagemaker create-endpoint-config --endpoint-config-name pm-epc-$STAMP \
  --production-variants "[{\"VariantName\":\"AllTraffic\",\"ModelName\":\"pm-model-$STAMP\",
     \"InstanceType\":\"ml.g5.xlarge\",\"InitialInstanceCount\":1,
     \"InferenceAmiVersion\":\"al2-ami-sagemaker-inference-gpu-3-1\",
     \"ModelDataDownloadTimeoutInSeconds\":1200,\"ContainerStartupHealthCheckTimeoutInSeconds\":1200}]" \
  --async-inference-config "{\"OutputConfig\":{\"S3OutputPath\":\"s3://$BUCKET/async/out/\",
     \"S3FailurePath\":\"s3://$BUCKET/async/fail/\"},
     \"ClientConfig\":{\"MaxConcurrentInvocationsPerInstance\":1}}" >/dev/null
if aws sagemaker describe-endpoint --endpoint-name $EP >/dev/null 2>&1; then
  aws sagemaker update-endpoint --endpoint-name $EP --endpoint-config-name pm-epc-$STAMP >/dev/null
else
  aws sagemaker create-endpoint --endpoint-name $EP --endpoint-config-name pm-epc-$STAMP >/dev/null
fi
sleep 30; aws sagemaker wait endpoint-in-service --endpoint-name $EP
aws sagemaker describe-endpoint --endpoint-name $EP --query "[EndpointStatus,EndpointConfigName]" --output text
