#!/usr/bin/env bash
# infra/cleanup-keep-model.sh [--yes]  -- delete everything EXCEPT what is needed to serve the model again
#
# KEPT (all you need for `infra/up.sh`; about $1.10/month, ~$1 of it the image):
#   - model package $PKG (Approved) in group $PROJECT
#   - its model.tar.gz (decoder + packed mT5 encoder) and its eval/<job>/ report in the bucket
#   - its container image in ECR (promptmusic:$IMAGE_TAG)
#   - role $ROLE_NAME (the endpoint runs as it)
#   - the budget alert (free, a safety net)
# DELETED:
#   - endpoint, endpoint configs, models, autoscaling, alarms, endpoint logs
#   - every other model package version (1-5 etc.)
#   - every other S3 object: data/, models/, checkpoints/, build/, async/, other training outputs
#   - untagged / other-tagged ECR images
#   - the GUI: EC2 instance promptmusic-studio, its security group, role and instance profile
#   - CodeBuild project, its log group and role (only needed to rebuild the image)
set -euo pipefail
cd "$(dirname "$0")/.."
source infra/env.sh
EP=${EP:-${PROJECT}-async}
CODEBUILD_PROJECT=${CODEBUILD_PROJECT:-prompt-music-image}
STUDIO=${STUDIO:-promptmusic-studio}

# ---- which model to keep ----------------------------------------------------------------
[ -z "${PKG:-}" ] && [ -f .pkg ] && source .pkg
if [ -z "${PKG:-}" ]; then
  PKG=$(aws sagemaker list-model-packages --model-package-group-name $PROJECT \
    --model-approval-status Approved --sort-by CreationTime --sort-order Descending \
    --query "ModelPackageSummaryList[0].ModelPackageArn" --output text)
fi
read -r MODEL_DATA KEEP_IMAGE STATUS < <(aws sagemaker describe-model-package --model-package-name "$PKG" \
  --query "[InferenceSpecification.Containers[0].ModelDataUrl,InferenceSpecification.Containers[0].Image,ModelApprovalStatus]" \
  --output text)
MODEL_KEY=${MODEL_DATA#s3://$BUCKET/}                       # training-output/<job>/output/model.tar.gz
JOB=$(echo "$MODEL_KEY" | cut -d/ -f2)
KEEP_TAG=${KEEP_IMAGE##*:}
[ "$STATUS" = Approved ] || { echo "ABORT: $PKG is $STATUS, not Approved"; exit 1; }
aws s3 ls "$MODEL_DATA" >/dev/null || { echo "ABORT: $MODEL_DATA not found"; exit 1; }

cat <<EOF
KEEP   model package  $PKG
KEEP   model file     $MODEL_DATA
KEEP   eval report    s3://$BUCKET/eval/$JOB/
KEEP   image          $KEEP_IMAGE
KEEP   role           $ROLE_NAME, budget promptmusic-monthly
DELETE endpoint $EP (+ configs, models, alarms, autoscaling), other package versions,
       all other objects in s3://$BUCKET, other ECR images, CodeBuild $CODEBUILD_PROJECT,
       EC2 $STUDIO (+ security group ${STUDIO}-sg, role ${STUDIO}-ec2)
EOF
if [ "${1:-}" != --yes ]; then
  read -r -p "Type 'delete' to continue: " ANSWER
  [ "$ANSWER" = delete ] || { echo "cancelled, nothing changed"; exit 0; }
fi

# ---- 1. endpoint and everything around it -----------------------------------------------
echo "== endpoint"
aws application-autoscaling deregister-scalable-target --service-namespace sagemaker \
  --resource-id endpoint/$EP/variant/AllTraffic \
  --scalable-dimension sagemaker:variant:DesiredInstanceCount 2>/dev/null || true
aws cloudwatch delete-alarms --alarm-names $EP-has-backlog $EP-idle
if aws sagemaker describe-endpoint --endpoint-name $EP >/dev/null 2>&1; then
  aws sagemaker delete-endpoint --endpoint-name $EP
  aws sagemaker wait endpoint-deleted --endpoint-name $EP || true
fi
for c in $(aws sagemaker list-endpoint-configs --name-contains pm- --query "EndpointConfigs[].EndpointConfigName" --output text); do
  aws sagemaker delete-endpoint-config --endpoint-config-name $c; done
for m in $(aws sagemaker list-models --name-contains pm- --query "Models[].ModelName" --output text); do
  aws sagemaker delete-model --model-name $m; done
aws logs delete-log-group --log-group-name /aws/sagemaker/Endpoints/$EP 2>/dev/null || true

# ---- 2. other model package versions -----------------------------------------------------
echo "== model registry (keeping $PKG)"
for p in $(aws sagemaker list-model-packages --model-package-group-name $PROJECT \
           --query "ModelPackageSummaryList[].ModelPackageArn" --output text); do
  [ "$p" = "$PKG" ] || { aws sagemaker delete-model-package --model-package-name $p && echo "  deleted $p"; }
done

# ---- 3. S3: everything except the model file and its eval report ------------------------
echo "== s3 (keeping $MODEL_KEY, eval/$JOB/)"
aws s3 rm s3://$BUCKET --recursive --only-show-errors \
  --exclude "$MODEL_KEY" --exclude "eval/$JOB/*"

# ---- 4. ECR: only the image the package uses ---------------------------------------------
echo "== ecr (keeping :$KEEP_TAG)"
IDS=$(aws ecr list-images --repository-name $ECR_REPO --output json \
  --query "imageIds[?imageTag!='$KEEP_TAG']")
KEEP_DIGEST=$(aws ecr describe-images --repository-name $ECR_REPO --image-ids imageTag=$KEEP_TAG \
  --query "imageDetails[0].imageDigest" --output text)
IDS=$(echo "$IDS" | python3 -c "
import json,sys; ids=json.load(sys.stdin)
print(json.dumps([i for i in ids if i.get('imageDigest')!='$KEEP_DIGEST']))")
[ "$IDS" != "[]" ] && aws ecr batch-delete-image --repository-name $ECR_REPO --image-ids "$IDS" \
  --query "length(imageIds)" --output text | sed 's/^/  images deleted: /' || true

# ---- 5. the GUI on EC2 ---------------------------------------------------------------------
echo "== ec2 $STUDIO"
INST=$(aws ec2 describe-instances --filters Name=tag:Name,Values=$STUDIO \
  Name=instance-state-name,Values=pending,running,stopping,stopped \
  --query "Reservations[].Instances[].InstanceId" --output text)
if [ -n "$INST" ]; then
  aws ec2 terminate-instances --instance-ids $INST >/dev/null
  aws ec2 wait instance-terminated --instance-ids $INST && echo "  terminated $INST"
fi
SG=$(aws ec2 describe-security-groups --filters Name=group-name,Values=${STUDIO}-sg \
  --query "SecurityGroups[0].GroupId" --output text 2>/dev/null || echo None)
[ "$SG" != None ] && aws ec2 delete-security-group --group-id $SG && echo "  deleted security group $SG" || true

# ---- 6. roles that are no longer needed (the endpoint role $ROLE_NAME is kept) ------------
delete_role() {
  local r=$1
  aws iam get-role --role-name $r >/dev/null 2>&1 || return 0
  for p in $(aws iam list-role-policies --role-name $r --query PolicyNames --output text); do
    aws iam delete-role-policy --role-name $r --policy-name $p; done
  for p in $(aws iam list-attached-role-policies --role-name $r --query "AttachedPolicies[].PolicyArn" --output text); do
    aws iam detach-role-policy --role-name $r --policy-arn $p; done
  for ip in $(aws iam list-instance-profiles-for-role --role-name $r --query "InstanceProfiles[].InstanceProfileName" --output text); do
    aws iam remove-role-from-instance-profile --instance-profile-name $ip --role-name $r
    aws iam delete-instance-profile --instance-profile-name $ip; done
  aws iam delete-role --role-name $r && echo "  deleted role $r"
}
echo "== codebuild $CODEBUILD_PROJECT and roles"
aws codebuild delete-project --name $CODEBUILD_PROJECT >/dev/null 2>&1 || true
aws logs delete-log-group --log-group-name /aws/codebuild/$CODEBUILD_PROJECT 2>/dev/null || true
delete_role ${STUDIO}-ec2
delete_role ${PROJECT}-codebuild

# ---- what is left ---------------------------------------------------------------------------
echo "== left"
aws s3 ls s3://$BUCKET --recursive --human-readable
aws ecr describe-images --repository-name $ECR_REPO --query "imageDetails[].[imageTags[0],imageSizeInBytes]" --output text
aws sagemaker list-model-packages --model-package-group-name $PROJECT \
  --query "ModelPackageSummaryList[].[ModelPackageVersion,ModelApprovalStatus]" --output text
echo "export PKG=$PKG" > .pkg
echo "done. bring it back with: infra/up.sh"
