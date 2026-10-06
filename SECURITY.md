# Security review (Phase 12, verified 2026-10-06)

## Controls in place (each checked on the live system)

| Area | Control | How it was verified |
| --- | --- | --- |
| Secrets | DB and dashboard passwords only in SSM Parameter Store (SecureString), generated randomly, never printed | Searched logs, code and config on the server for the actual password values: 0 matches. All 16 git commits scanned: no keys, passwords or private keys |
| AWS credentials | None stored anywhere; EC2 uses an IAM role; IMDSv2 required | `HttpTokens=required`, no key pair |
| IAM | `retailbank-least-privilege`: S3 Get/Put only on `bronze/`, `profiles/`, `quarantine/`; SSM read only `/retailbank/db/*`, `/retailbank/dashboard/*`; logs only `/retailbank/*`; no wildcard resources; no delete rights | Policy read back; writes outside allowed prefixes denied; other parameters denied |
| Database network | RDS not public; private subnets with no internet route; port 3306 open only to the app security group | `PubliclyAccessible=false`, SG rules read back |
| Database access | App user `retailbank_app` sees only the `retailbank` database; TLS required | `SHOW DATABASES`; cipher `TLS_AES_256_GCM_SHA384` |
| Encryption | RDS encrypted at rest; S3 AES-256 by default; S3 policy denies non-HTTPS; TLS to RDS | Read back from AWS |
| S3 | All public access blocked; versioning on (raw files recoverable) | Read back from AWS |
| Server | Only ports 80 (ours) and 8080 (PayRecon reference) open; SSH off; admin only via SSM; dashboard runs as non-root `retailbank` user with `NoNewPrivileges`, `ProtectSystem` | `ss -ltn`, `ps`, systemd unit |
| Settings file | `/etc/retailbank/retailbank.env` has no secrets; mode 640 root:retailbank | `stat` |
| Dashboard | Login with SSM password, timing-safe compare, 1 s delay on failure; stack traces hidden from visitors; CSV downloads neutralise spreadsheet formulas | AppTest tests; systemd flags |
| Data protection | RDS deletion protection on; daily automated backup | Read back from AWS |

## Known MVP limitations (stated honestly)

1. **HTTP, not HTTPS.** The dashboard is plain HTTP on port 80. Production: an Application Load Balancer with an ACM certificate, or CloudFront.
2. **Dashboard open to the internet** (protected only by the shared password). Production: restrict the security group to the bank's IP ranges or use SSO.
3. **One shared dashboard login**, no per-user accounts or roles.
4. **Shared EC2 role.** The instance also hosts our PayRecon reference app, so its role can read both projects' parameters. Production: one instance or task role per application.
5. **One DB user for schema and runtime.** It has CREATE/DROP to apply the schema. Production: a separate migration user, and a runtime user with only SELECT/INSERT/UPDATE/DELETE.
6. **No alarm notifications.** The CloudWatch alarm changes state but is not wired to SNS email/SMS yet.
7. **Synthetic PII.** Names, emails and phones are fictitious. Real data would need column masking and tighter access to `dq_quarantine.raw_record`.
