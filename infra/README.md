# AWS infrastructure (ap-southeast-2, account 748348797173)

Created in Phase 3. Reused resources come from the team's earlier PayRecon practice project; they are noted as such. No passwords are stored here.

## What exists and why

| Resource | Name / ID | New or reused | Purpose |
| --- | --- | --- | --- |
| S3 bucket | `retailbank-data-748348797173` | New | Bronze zone (`bronze/`), profiles (`profiles/`), quarantine copies (`quarantine/`). Public access blocked, AES-256 encryption, versioning on, HTTPS-only bucket policy |
| RDS MySQL 8.4 | `payrecon-db` (private, encrypted) | Reused instance | New database `retailbank`; app user `retailbank_app` (TLS required, rights on `retailbank.*` only) |
| EC2 | `i-06fd16a1e81a1cdbb` (t3.micro, 3.107.194.103) | Reused | Runs the pipeline and the Streamlit dashboard. No SSH; admin via SSM Run Command |
| VPC + subnets | `vpc-0611eb38d0fb843c5` | Reused | EC2 in the public subnet, RDS in two private subnets with no internet route |
| Security group (app) | `sg-0c8b701c0ac03b5aa` | Reused | Inbound 80 (our dashboard) and 8080 (PayRecon reference dashboard) |
| Security group (db) | `sg-0387cf15fe106684d` | Reused | Inbound 3306 only from the app security group |
| IAM role | `payrecon-ec2-role` | Reused | New inline policy `retailbank-least-privilege` (below) |
| SSM parameters | `/retailbank/db/app_password`, `/retailbank/dashboard/password` | New | SecureString passwords, generated randomly, never printed |
| CloudWatch logs | `/retailbank/pipeline`, `/retailbank/dashboard` | New | 14-day retention |
| CloudWatch alarm | `retailbank-pipeline-errors` | New | Fires when the pipeline log contains `ERROR` (metric `RetailBank/PipelineErrors`) |

## IAM policy `retailbank-least-privilege` (on the EC2 role)

| Allows | On |
| --- | --- |
| `s3:ListBucket` | the retailbank bucket |
| `s3:GetObject`, `s3:PutObject` | only `bronze/*`, `profiles/*`, `quarantine/*` |
| `ssm:GetParameter` | only `/retailbank/db/*`, `/retailbank/dashboard/*` |
| `logs:CreateLogStream`, `PutLogEvents` | only `/retailbank/*` log groups |

The existing explicit DENY on other projects' parameters was extended to allow `/retailbank/*` too. No delete permissions: the pipeline can never remove raw files.

## Verified (from the EC2 instance)

- S3 write to `bronze/` allowed; write outside the allowed prefixes denied
- SSM read of the retailbank password allowed
- CloudWatch write to `/retailbank/pipeline` allowed
- `retailbank_app` connects to RDS over TLS (`TLS_AES_256_GCM_SHA384`) and sees only the `retailbank` database
- PayRecon dashboard moved to port 8080 and healthy; port 80 free for this project

## Deployment (Phase 9)

`infra/deploy.sh` (idempotent, run as root through SSM Run Command) creates the `retailbank` service user, pulls this repo to `/opt/retailbank/app`, builds `/opt/retailbank/venv` from `requirements.txt`, writes `/etc/retailbank/retailbank.env` (no secrets, mode 640), applies the schema and KPI views, installs the `retailbank-dashboard` systemd service on port 80 (non-root, auto-restart), adds the CloudWatch agent config `retailbank-cloudwatch.json`, and health-checks the dashboard. `retailbank-run <batch_id>` runs the pipeline as the service user.

| Endpoint | URL |
| --- | --- |
| RetailBank dashboard | http://3.107.194.103 |
| PayRecon reference dashboard | http://3.107.194.103:8080 |
