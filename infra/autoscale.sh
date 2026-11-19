#!/usr/bin/env bash
# infra/autoscale.sh  -- async endpoint: 0 -> 1 when a request arrives, 1 -> 0 after IDLE_MINUTES idle
#
# Scale-in is driven by "no request queued or in progress for IDLE_MINUTES" (alarm $EP-idle on the
# per-minute MAXIMUM of ApproximateBacklogSize), not by target tracking: a target-tracking policy on
# the backlog sees ~0 even while songs are being made (each takes ~20-60 s, the per-minute average
# stays < 1), so it scaled in 15 min after waking up regardless of use.
set -euo pipefail
source "$(dirname "$0")/env.sh"
EP=${EP:-${PROJECT}-async}; RID=endpoint/$EP/variant/AllTraffic
DIM=sagemaker:variant:DesiredInstanceCount
IDLE_MINUTES=${IDLE_MINUTES:-30}

aws application-autoscaling register-scalable-target --service-namespace sagemaker \
  --resource-id $RID --scalable-dimension $DIM --min-capacity 0 --max-capacity 1

# The old target-tracking policy (scaled in on a 15-min low average) is replaced by the idle alarm below.
aws application-autoscaling delete-scaling-policy --policy-name backlog-tracking \
  --service-namespace sagemaker --resource-id $RID --scalable-dimension $DIM 2>/dev/null || true

# 0 -> 1: a request is waiting and there is no instance.
UP_ARN=$(aws application-autoscaling put-scaling-policy --policy-name scale-from-zero \
  --service-namespace sagemaker --resource-id $RID --scalable-dimension $DIM --policy-type StepScaling \
  --step-scaling-policy-configuration '{"AdjustmentType":"ChangeInCapacity","MetricAggregationType":"Average","Cooldown":300,"StepAdjustments":[{"MetricIntervalLowerBound":0,"ScalingAdjustment":1}]}' \
  --query PolicyARN --output text)
aws cloudwatch put-metric-alarm --alarm-name $EP-has-backlog \
  --namespace AWS/SageMaker --metric-name HasBacklogWithoutCapacity \
  --dimensions Name=EndpointName,Value=$EP --statistic Average --period 60 \
  --evaluation-periods 1 --threshold 1 --comparison-operator GreaterThanOrEqualToThreshold \
  --treat-missing-data notBreaching --alarm-actions $UP_ARN

# 1 -> 0: nothing queued or running in any minute of the last IDLE_MINUTES minutes.
# Missing data counts as idle, so a silent metric can never keep the instance (and the bill) running.
DOWN_ARN=$(aws application-autoscaling put-scaling-policy --policy-name scale-to-zero-when-idle \
  --service-namespace sagemaker --resource-id $RID --scalable-dimension $DIM --policy-type StepScaling \
  --step-scaling-policy-configuration '{"AdjustmentType":"ExactCapacity","MetricAggregationType":"Maximum","Cooldown":60,"StepAdjustments":[{"MetricIntervalUpperBound":0,"ScalingAdjustment":0}]}' \
  --query PolicyARN --output text)
aws cloudwatch put-metric-alarm --alarm-name $EP-idle \
  --namespace AWS/SageMaker --metric-name ApproximateBacklogSize \
  --dimensions Name=EndpointName,Value=$EP --statistic Maximum --period 60 \
  --evaluation-periods $IDLE_MINUTES --datapoints-to-alarm $IDLE_MINUTES \
  --threshold 0.5 --comparison-operator LessThanThreshold \
  --treat-missing-data breaching --alarm-actions $DOWN_ARN

echo "autoscaling on $EP: 0 -> 1 on a waiting request, 1 -> 0 after $IDLE_MINUTES idle minutes"
