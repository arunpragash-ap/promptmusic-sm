#!/usr/bin/env bash
# infra/run_all.sh [smoke|full]  -- prepare (if needed) -> train -> print next steps
#   smoke: 2 000 pieces, 200 steps, on-demand      full: 20 000 pieces, STEPS (default 3000)
set -euo pipefail
cd "$(dirname "$0")/.."
source infra/env.sh
MODE=${1:-smoke}
if [ "$MODE" = smoke ]; then export LIMIT=2000 DATAVER=smoke; TEMPLATE=infra/train-smoke.json
else export LIMIT=${LIMIT:-20000} DATAVER=${DATAVER:-v1} STEPS=${STEPS:-3000}; TEMPLATE=infra/train-full.json; fi

if ! aws s3 ls s3://$BUCKET/data/tokens/$DATAVER/validation_0.json >/dev/null 2>&1; then
  J=pm-prepare-$STAMP
  infra/render.sh infra/prepare-job.json > /tmp/prepare.json
  aws sagemaker create-processing-job --cli-input-json file:///tmp/prepare.json >/dev/null
  echo "prepare: $J"
  aws sagemaker wait processing-job-completed-or-stopped --processing-job-name $J || true
  aws sagemaker describe-processing-job --processing-job-name $J --query "[ProcessingJobStatus,FailureReason]" --output text
fi

J=pm-train-$STAMP
infra/render.sh $TEMPLATE > /tmp/train.json
aws sagemaker create-training-job --cli-input-json file:///tmp/train.json >/dev/null
echo "training: $J   (logs: aws logs tail /aws/sagemaker/TrainingJobs --log-stream-name-prefix $J --follow)"
aws sagemaker wait training-job-completed-or-stopped --training-job-name $J || true
aws sagemaker describe-training-job --training-job-name $J \
  --query "[TrainingJobStatus,FailureReason,BillableTimeInSeconds,ModelArtifacts.S3ModelArtifacts]" --output text
echo "next: listen to s3://$BUCKET/training-output/$J/output/output.tar.gz (samples), then"
echo "      PKG=\$(infra/register.sh $J) && infra/deploy.sh \$PKG && infra/autoscale.sh"
