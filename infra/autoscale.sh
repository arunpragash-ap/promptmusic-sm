#!/usr/bin/env bash
# infra/autoscale.sh  -- async endpoint: 1 -> 0 when idle, 0 -> 1 when a request arrives
set -euo pipefail
source "$(dirname "$0")/env.sh"
EP=${EP:-${PROJECT}-async}; RID=endpoint/$EP/variant/AllTraffic
DIM=sagemaker:variant:DesiredInstanceCount
aws application-autoscaling register-scalable-target --service-namespace sagemaker \
  --resource-id $RID --scalable-dimension $DIM --min-capacity 0 --max-capacity 1
aws application-autoscaling put-scaling-policy --policy-name backlog-tracking \
  --service-namespace sagemaker --resource-id $RID --scalable-dimension $DIM \
  --policy-type TargetTrackingScaling --query PolicyARN --output text \
  --target-tracking-scaling-policy-configuration "{\"TargetValue\":1.0,
    \"CustomizedMetricSpecification\":{\"MetricName\":\"ApproximateBacklogSizePerInstance\",
      \"Namespace\":\"AWS/SageMaker\",\"Dimensions\":[{\"Name\":\"EndpointName\",\"Value\":\"$EP\"}],
      \"Statistic\":\"Average\"},\"ScaleInCooldown\":900,\"ScaleOutCooldown\":60}"
STEP_ARN=$(aws application-autoscaling put-scaling-policy --policy-name scale-from-zero \
  --service-namespace sagemaker --resource-id $RID --scalable-dimension $DIM --policy-type StepScaling \
  --step-scaling-policy-configuration '{"AdjustmentType":"ChangeInCapacity","MetricAggregationType":"Average","Cooldown":300,"StepAdjustments":[{"MetricIntervalLowerBound":0,"ScalingAdjustment":1}]}' \
  --query PolicyARN --output text)
aws cloudwatch put-metric-alarm --alarm-name $EP-has-backlog \
  --namespace AWS/SageMaker --metric-name HasBacklogWithoutCapacity \
  --dimensions Name=EndpointName,Value=$EP --statistic Average --period 60 \
  --evaluation-periods 1 --threshold 1 --comparison-operator GreaterThanOrEqualToThreshold \
  --treat-missing-data notBreaching --alarm-actions $STEP_ARN
echo "autoscaling on $EP: min 0, max 1"
