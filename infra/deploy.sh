#!/usr/bin/env bash
# infra/deploy.sh <model-package-arn>  -- create or update the async endpoint
#
# Tries each instance type in INSTANCE_TYPES in order until one gets capacity.
# Run infra/autoscale.sh afterwards: autoscaling must be removed before an
# update that changes the instance type, so this script removes it.
set -euo pipefail
source "$(dirname "$0")/env.sh"
PKG=$1; EP=${EP:-${PROJECT}-async}
INSTANCE_TYPES=${INSTANCE_TYPES:-"ml.g5.xlarge ml.g6.xlarge"}
RID=endpoint/$EP/variant/AllTraffic; DIM=sagemaker:variant:DesiredInstanceCount

EXISTS=0; aws sagemaker describe-endpoint --endpoint-name $EP >/dev/null 2>&1 && EXISTS=1
if [ $EXISTS = 1 ]; then
  # "The variant(s) must be deregistered as scalable targets ... before they can
  #  ... have their instance type updated"
  aws application-autoscaling deregister-scalable-target --service-namespace sagemaker \
    --resource-id $RID --scalable-dimension $DIM 2>/dev/null && echo "autoscaling removed (re-run infra/autoscale.sh)" || true
  aws cloudwatch delete-alarms --alarm-names $EP-has-backlog
fi

for IT in $INSTANCE_TYPES; do
  NAME=pm-$(date +%Y%m%d-%H%M%S)
  aws sagemaker create-model --model-name $NAME --execution-role-arn $ROLE_ARN \
    --primary-container ModelPackageName=$PKG >/dev/null
  # InferenceAmiVersion: the default hosting AMI has driver 470 (CUDA 11.4); torch 2.6 needs 12.x
  aws sagemaker create-endpoint-config --endpoint-config-name $NAME \
    --production-variants "[{\"VariantName\":\"AllTraffic\",\"ModelName\":\"$NAME\",
       \"InstanceType\":\"$IT\",\"InitialInstanceCount\":1,
       \"InferenceAmiVersion\":\"al2-ami-sagemaker-inference-gpu-3-1\",
       \"ModelDataDownloadTimeoutInSeconds\":1200,\"ContainerStartupHealthCheckTimeoutInSeconds\":1200}]" \
    --async-inference-config "{\"OutputConfig\":{\"S3OutputPath\":\"s3://$BUCKET/async/out/\",
       \"S3FailurePath\":\"s3://$BUCKET/async/fail/\"},
       \"ClientConfig\":{\"MaxConcurrentInvocationsPerInstance\":1}}" >/dev/null
  echo "trying $IT ($NAME)"
  if [ $EXISTS = 1 ]; then
    aws sagemaker update-endpoint --endpoint-name $EP --endpoint-config-name $NAME >/dev/null
  else
    aws sagemaker create-endpoint --endpoint-name $EP --endpoint-config-name $NAME >/dev/null
  fi
  sleep 30
  aws sagemaker wait endpoint-in-service --endpoint-name $EP 2>/dev/null || true
  # A failed UPDATE rolls back and is InService again on the OLD config; a failed CREATE is Failed.
  read -r STATUS LIVE < <(aws sagemaker describe-endpoint --endpoint-name $EP \
    --query "[EndpointStatus,EndpointConfigName]" --output text)
  if [ "$STATUS" = InService ] && [ "$LIVE" = "$NAME" ]; then
    echo "InService on $LIVE ($IT)"; exit 0
  fi
  echo "  failed on $IT: $(aws sagemaker describe-endpoint --endpoint-name $EP --query FailureReason --output text | cut -c1-160)"
  if [ "$STATUS" = Failed ]; then aws sagemaker delete-endpoint --endpoint-name $EP; sleep 20; EXISTS=0; fi
done
echo "DEPLOY FAILED on all of: $INSTANCE_TYPES"; exit 1
