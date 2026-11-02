#!/usr/bin/env bash
# infra/cleanup.sh [--all]  -- stop paying for compute; --all also deletes data, image, roles
set -euo pipefail
source "$(dirname "$0")/env.sh"
EP=${EP:-${PROJECT}-async}
aws application-autoscaling deregister-scalable-target --service-namespace sagemaker \
  --resource-id endpoint/$EP/variant/AllTraffic \
  --scalable-dimension sagemaker:variant:DesiredInstanceCount 2>/dev/null || true
aws cloudwatch delete-alarms --alarm-names $EP-has-backlog
aws sagemaker delete-endpoint --endpoint-name $EP 2>/dev/null || true
for c in $(aws sagemaker list-endpoint-configs --name-contains pm-epc --query "EndpointConfigs[].EndpointConfigName" --output text); do
  aws sagemaker delete-endpoint-config --endpoint-config-name $c; done
for m in $(aws sagemaker list-models --name-contains pm-model --query "Models[].ModelName" --output text); do
  aws sagemaker delete-model --model-name $m; done
echo "running jobs (should be empty):"
aws sagemaker list-training-jobs --status-equals InProgress --query "TrainingJobSummaries[].TrainingJobName" --output text
aws sagemaker list-processing-jobs --status-equals InProgress --query "ProcessingJobSummaries[].ProcessingJobName" --output text
[ "${1:-}" = "--all" ] || { echo "compute stopped; data, image and registry kept"; exit 0; }

# --- everything else: PERMANENT ---
for p in $(aws sagemaker list-model-packages --model-package-group-name $PROJECT --query "ModelPackageSummaryList[].ModelPackageArn" --output text); do
  aws sagemaker delete-model-package --model-package-name $p; done
aws sagemaker delete-model-package-group --model-package-group-name $PROJECT
aws s3 rm s3://$BUCKET --recursive --only-show-errors && aws s3api delete-bucket --bucket $BUCKET
aws ecr delete-repository --repository-name $ECR_REPO --force >/dev/null
aws codebuild delete-project --name ${PROJECT}-image
aws logs delete-log-group --log-group-name /aws/codebuild/${PROJECT}-image 2>/dev/null || true
for r in $ROLE_NAME ${PROJECT}-codebuild; do
  for p in $(aws iam list-role-policies --role-name $r --query PolicyNames --output text); do aws iam delete-role-policy --role-name $r --policy-name $p; done
  for p in $(aws iam list-attached-role-policies --role-name $r --query "AttachedPolicies[].PolicyArn" --output text); do aws iam detach-role-policy --role-name $r --policy-arn $p; done
  aws iam delete-role --role-name $r; done
aws budgets delete-budget --account-id $ACCOUNT_ID --budget-name promptmusic-monthly
echo "everything deleted"
