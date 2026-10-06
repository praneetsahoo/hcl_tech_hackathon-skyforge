#!/usr/bin/env bash
# Deploy / update RetailBank on the EC2 instance. Safe to run again (idempotent).
# Run as root via SSM Run Command:  bash /opt/retailbank/app/infra/deploy.sh
set -euo pipefail

APP_DIR=/opt/retailbank/app
VENV=/opt/retailbank/venv
REPO=https://github.com/praneetsahoo/hcl_tech_hackathon-skyforge.git
ENV_FILE=/etc/retailbank/retailbank.env
LOG_DIR=/var/log/retailbank

echo "== 1. service user (no login shell, no sudo)"
id retailbank &>/dev/null || useradd --system --home-dir /var/lib/retailbank --create-home --shell /sbin/nologin retailbank

echo "== 2. code"
if [ -d "$APP_DIR/.git" ]; then git -C "$APP_DIR" pull -q; else git clone -q "$REPO" "$APP_DIR"; fi
git -C "$APP_DIR" log --oneline -1

echo "== 3. python environment (versions pinned in requirements.txt)"
[ -x "$VENV/bin/python" ] || python3.11 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -r "$APP_DIR/requirements.txt"
"$VENV/bin/python" -c "import pandas, streamlit; print('pandas', pandas.__version__, '| streamlit', streamlit.__version__)"

echo "== 4. RDS TLS certificate + settings file (NO passwords: they stay in SSM)"
mkdir -p /etc/retailbank "$LOG_DIR"
[ -f /opt/retailbank/rds-ca.pem ] || curl -sSf -o /opt/retailbank/rds-ca.pem \
    https://truststore.pki.rds.amazonaws.com/ap-southeast-2/ap-southeast-2-bundle.pem
cat > "$ENV_FILE" <<'EOF'
AWS_REGION=ap-southeast-2
RB_S3_BUCKET=retailbank-data-748348797173
RB_DB_HOST=payrecon-db.c10guuuqcerb.ap-southeast-2.rds.amazonaws.com
RB_DB_PORT=3306
RB_DB_NAME=retailbank
RB_DB_USER=retailbank_app
RB_DB_PASSWORD_PARAM=/retailbank/db/app_password
RB_DASHBOARD_PASSWORD_PARAM=/retailbank/dashboard/password
RB_DB_SSL_CA=/opt/retailbank/rds-ca.pem
RB_LOG_FILE=/var/log/retailbank/pipeline.log
HOME=/var/lib/retailbank
EOF
chown root:retailbank "$ENV_FILE"
chmod 640 "$ENV_FILE"          # no secrets inside, but only the app needs to read it
touch "$LOG_DIR/pipeline.log" "$LOG_DIR/dashboard.log"
chown -R retailbank:retailbank "$LOG_DIR"

echo "== 5. database schema + KPI views"
sudo -u retailbank env $(grep -v '^#' "$ENV_FILE" | xargs) "$VENV/bin/python" -m pipeline.db apply-schema

echo "== 5b. official Day 1 / Day 2 files into the S3 landing zone (skips files already there)"
sudo -u retailbank env $(grep -v '^#' "$ENV_FILE" | xargs) "$VENV/bin/python" -m scripts.seed_landing | tail -2

echo "== 6. dashboard service on port 80"
install -m 644 "$APP_DIR/infra/retailbank-dashboard.service" /etc/systemd/system/retailbank-dashboard.service
install -m 755 "$APP_DIR/infra/run_pipeline.sh" /usr/local/bin/retailbank-run
systemctl daemon-reload
systemctl enable -q retailbank-dashboard
systemctl restart retailbank-dashboard

echo "== 7. ship logs to CloudWatch (own file name, so it never replaces PayRecon's config)"
/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl -a append-config -m ec2 \
    -c "file:$APP_DIR/infra/retailbank-cloudwatch.json" -s >/dev/null
/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl -a status | grep -o '"status": "[a-z]*"' | head -1

echo "== 8. health check"
for i in $(seq 1 30); do
  if curl -sf http://localhost:80/_stcore/health >/dev/null; then echo "dashboard healthy on :80"; exit 0; fi
  sleep 2
done
echo "ERROR dashboard did not become healthy"; journalctl -u retailbank-dashboard -n 30 --no-pager; exit 1
