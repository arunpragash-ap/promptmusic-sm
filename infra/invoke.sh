#!/usr/bin/env bash
# infra/invoke.sh <request.json> [out.zip]  -- async request, wait, download, unzip
set -euo pipefail
source "$(dirname "$0")/env.sh"
EP=${EP:-${PROJECT}-async}
REQ=$1; OUTZIP=${2:-song.zip}
KEY=async/in/req-$(date +%s).json
aws s3 cp "$REQ" s3://$BUCKET/$KEY --content-type application/json --only-show-errors
read -r OUT FAIL < <(aws sagemaker-runtime invoke-endpoint-async --endpoint-name $EP \
  --input-location s3://$BUCKET/$KEY --content-type application/json \
  --invocation-timeout-seconds 1800 --query "[OutputLocation,FailureLocation]" --output text)
echo "waiting for $OUT"
T0=$(date +%s)
until aws s3 ls "$OUT" >/dev/null 2>&1; do
  if aws s3 ls "$FAIL" >/dev/null 2>&1; then echo "FAILED:"; aws s3 cp "$FAIL" -; exit 1; fi
  sleep 15
done
echo "ready after $(( $(date +%s) - T0 )) s"
aws s3 cp "$OUT" "$OUTZIP" --only-show-errors
rm -rf "${OUTZIP%.zip}" && unzip -q -o "$OUTZIP" -d "${OUTZIP%.zip}" && ls -la "${OUTZIP%.zip}"
