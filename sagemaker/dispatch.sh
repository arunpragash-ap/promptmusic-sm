#!/usr/bin/env bash
# SageMaker runs:  <image> train   (training jobs)
#                  <image> serve   (endpoints)
# Processing jobs set their own ContainerEntrypoint.
set -euo pipefail
case "${1:-}" in
  train) shift; exec python /opt/program/sagemaker/entry/train_sm.py "$@" ;;
  serve) shift; cd /opt/program/sagemaker
         exec gunicorn --bind 0.0.0.0:8080 --workers 1 --threads 2 \
              --timeout 3600 --graceful-timeout 60 entry.serve:app ;;
  *)     exec "$@" ;;
esac
