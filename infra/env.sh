# infra/env.sh  --  source this file:  source infra/env.sh
export AWS_REGION=ap-south-1
export AWS_DEFAULT_REGION=$AWS_REGION
export ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export PROJECT=promptmusic
export BUCKET=sagemaker-${PROJECT}-${ACCOUNT_ID}
export ROLE_NAME=${PROJECT}-sagemaker-exec
export ROLE_ARN=arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}
export ECR_REPO=${PROJECT}
export IMAGE_TAG=${IMAGE_TAG:-v1}
export IMAGE_URI=${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO}:${IMAGE_TAG}
export BASE_IMAGE=763104351884.dkr.ecr.${AWS_REGION}.amazonaws.com/pytorch-training:2.6.0-gpu-py312-cu126-ubuntu22.04-sagemaker
export STAMP=${STAMP:-$(date +%Y%m%d-%H%M%S)}
