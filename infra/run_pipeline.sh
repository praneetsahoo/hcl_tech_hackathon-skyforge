#!/usr/bin/env bash
# Run one batch as the service user with the deployed settings.
#   retailbank-run day1
set -euo pipefail
[ $# -eq 1 ] || { echo "usage: retailbank-run <batch_id>"; exit 2; }
cd /opt/retailbank/app
exec sudo -u retailbank env $(grep -v '^#' /etc/retailbank/retailbank.env | xargs) \
    /opt/retailbank/venv/bin/python -m pipeline.run_pipeline --batch-id "$1"
