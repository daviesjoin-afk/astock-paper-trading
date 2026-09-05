#!/usr/bin/env bash
set -euo pipefail

curl --fail --silent --show-error --max-time 10 \
  http://127.0.0.1:8600/api/health
printf '\n'
