#!/usr/bin/env bash
# infra/render.sh <template.json>  -- fill STAMP/ROLE_ARN/IMAGE_URI/BUCKET/LIMIT from the environment
sed -e "s|STAMP|$STAMP|g" -e "s|ROLE_ARN|$ROLE_ARN|g" -e "s|IMAGE_URI|$IMAGE_URI|g" \
    -e "s|BUCKET|$BUCKET|g" -e "s|LIMIT|${LIMIT:-0}|g" -e "s|DATAVER|${DATAVER:-v1}|g" -e "s|STEPS|${STEPS:-null}|g" "$1"
