-- RetailBank curated layer (gold) + pipeline metadata, MySQL 8.
-- Safe to run repeatedly: every table uses CREATE TABLE IF NOT EXISTS.
-- Money is DECIMAL (exact), never FLOAT.

-- =====================================================================
-- DIMENSIONS (SCD Type 2)
-- One row per VERSION of a customer / product / branch.
--   valid_from / valid_to : the period this version was true
--   is_current            : 1 for the latest version
--   row_hash              : SHA-256 of the tracked attributes; if the hash
--                           is unchanged, a re-run creates no new version
--   current_key           : equals the business key only on the current row
--                           (NULL otherwise). Its UNIQUE index guarantees at
--                           most ONE current version per business key,
--                           because MySQL unique indexes ignore NULLs.
-- =====================================================================

CREATE TABLE IF NOT EXISTS dim_customer (
    customer_sk        INT AUTO_INCREMENT PRIMARY KEY,
    customer_id        VARCHAR(20)  NOT NULL,
    customer_name      VARCHAR(100) NOT NULL,
    email              VARCHAR(150) NULL,
    email_valid        TINYINT(1)   NOT NULL,
    phone              VARCHAR(20)  NULL,
    phone_valid        TINYINT(1)   NOT NULL,
    account_id         VARCHAR(20)  NOT NULL,
    gender             VARCHAR(10)  NOT NULL,          -- Male / Female / Other / Unknown
    dob                DATE         NULL,
    address            VARCHAR(200) NULL,
    kyc_status         VARCHAR(10)  NOT NULL,          -- Verified / Pending / Rejected
    registration_date  DATE         NULL,
    valid_from         DATE         NOT NULL,
    valid_to           DATE         NOT NULL DEFAULT '9999-12-31',
    is_current         TINYINT(1)   NOT NULL DEFAULT 1,
    row_hash           CHAR(64)     NOT NULL,
    batch_id           VARCHAR(10)  NOT NULL,
    current_key        VARCHAR(20)  AS (IF(is_current = 1, customer_id, NULL)) STORED,
    UNIQUE KEY uq_customer_current (current_key),
    UNIQUE KEY uq_customer_version (customer_id, valid_from),
    KEY ix_customer_account (account_id, is_current)
);

CREATE TABLE IF NOT EXISTS dim_product (
    product_sk    INT AUTO_INCREMENT PRIMARY KEY,
    product_id    VARCHAR(20)   NOT NULL,
    product_name  VARCHAR(100)  NULL,
    product_type  VARCHAR(30)   NOT NULL,              -- Savings Account / Loan / Credit Card / Fixed Deposit
    category      VARCHAR(20)   NOT NULL,              -- Retail / SME / Corporate
    price         DECIMAL(15,2) NOT NULL,
    launch_date   DATE          NULL,
    is_active     TINYINT(1)    NOT NULL,
    vendor_name   VARCHAR(100)  NULL,
    valid_from    DATE          NOT NULL,
    valid_to      DATE          NOT NULL DEFAULT '9999-12-31',
    is_current    TINYINT(1)    NOT NULL DEFAULT 1,
    row_hash      CHAR(64)      NOT NULL,
    batch_id      VARCHAR(10)   NOT NULL,
    current_key   VARCHAR(20)   AS (IF(is_current = 1, product_id, NULL)) STORED,
    UNIQUE KEY uq_product_current (current_key),
    UNIQUE KEY uq_product_version (product_id, valid_from),
    CONSTRAINT ck_product_price CHECK (price >= 0)
);

CREATE TABLE IF NOT EXISTS dim_branch (
    branch_sk       INT AUTO_INCREMENT PRIMARY KEY,
    branch_id       VARCHAR(20)  NOT NULL,
    branch_name     VARCHAR(100) NOT NULL,
    location        VARCHAR(50)  NOT NULL,
    manager_name    VARCHAR(100) NOT NULL,             -- 'UNKNOWN' when missing (flagged)
    opened_date     DATE         NULL,
    region          VARCHAR(10)  NOT NULL,             -- North / South / East / West
    branch_type     VARCHAR(10)  NOT NULL,             -- Urban / Rural
    contact_number  VARCHAR(20)  NULL,
    valid_from      DATE         NOT NULL,
    valid_to        DATE         NOT NULL DEFAULT '9999-12-31',
    is_current      TINYINT(1)   NOT NULL DEFAULT 1,
    row_hash        CHAR(64)     NOT NULL,
    batch_id        VARCHAR(10)  NOT NULL,
    current_key     VARCHAR(20)  AS (IF(is_current = 1, branch_id, NULL)) STORED,
    UNIQUE KEY uq_branch_current (current_key),
    UNIQUE KEY uq_branch_version (branch_id, valid_from)
);

-- =====================================================================
-- FACT (grain: exactly ONE row per transaction_id)
-- The *_sk columns point at the dimension version that was valid at the
-- transaction time (point-in-time). A Day 2 correction UPDATEs this row;
-- the primary key makes a duplicate fact impossible.
-- =====================================================================

CREATE TABLE IF NOT EXISTS fact_transaction (
    transaction_id         VARCHAR(20)   NOT NULL PRIMARY KEY,
    account_id             VARCHAR(20)   NOT NULL,
    customer_sk            INT           NOT NULL,
    product_sk             INT           NOT NULL,
    branch_sk              INT           NOT NULL,
    customer_id            VARCHAR(20)   NOT NULL,
    product_id             VARCHAR(20)   NOT NULL,
    branch_id              VARCHAR(20)   NOT NULL,
    txn_ts                 DATETIME      NOT NULL,     -- source of truth for time
    txn_date               DATE          NOT NULL,
    amount                 DECIMAL(15,2) NOT NULL,     -- in the original currency
    currency               CHAR(3)       NOT NULL,
    amount_inr             DECIMAL(15,2) NULL,         -- NULL for non-INR rows (needs review)
    payment_method         VARCHAR(20)   NOT NULL,
    status                 VARCHAR(10)   NOT NULL,     -- Success / Failed / Pending
    is_refund              TINYINT(1)    NOT NULL,
    remarks                VARCHAR(100)  NULL,
    date_mismatch          TINYINT(1)    NOT NULL DEFAULT 0,
    needs_review           TINYINT(1)    NOT NULL DEFAULT 0,
    source_batch_id        VARCHAR(10)   NOT NULL,
    last_updated_batch_id  VARCHAR(10)   NOT NULL,
    row_hash               CHAR(64)      NOT NULL,
    CONSTRAINT fk_fact_customer FOREIGN KEY (customer_sk) REFERENCES dim_customer (customer_sk),
    CONSTRAINT fk_fact_product  FOREIGN KEY (product_sk)  REFERENCES dim_product  (product_sk),
    CONSTRAINT fk_fact_branch   FOREIGN KEY (branch_sk)   REFERENCES dim_branch   (branch_sk),
    CONSTRAINT ck_fact_amount CHECK (amount > 0),
    KEY ix_fact_customer_ts (customer_id, txn_ts),
    KEY ix_fact_account_ts  (account_id, txn_ts),
    KEY ix_fact_branch_date (branch_id, txn_date),
    KEY ix_fact_product     (product_id)
);

-- Old vs new values of every corrected transaction (audit + KPI 11).
CREATE TABLE IF NOT EXISTS fact_correction_log (
    correction_id   INT AUTO_INCREMENT PRIMARY KEY,
    transaction_id  VARCHAR(20) NOT NULL,
    batch_id        VARCHAR(10) NOT NULL,
    run_id          CHAR(36)    NOT NULL,
    old_values      JSON        NOT NULL,
    new_values      JSON        NOT NULL,
    corrected_at    DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY ix_correction_txn (transaction_id)
);

-- =====================================================================
-- PIPELINE METADATA AND DATA QUALITY
-- =====================================================================

-- One row per source file per batch: what landed in bronze (S3).
CREATE TABLE IF NOT EXISTS ingestion_log (
    batch_id     VARCHAR(10)  NOT NULL,
    file_name    VARCHAR(100) NOT NULL,
    entity       VARCHAR(20)  NOT NULL,
    row_count    INT          NOT NULL,
    size_bytes   INT          NOT NULL,
    sha256       CHAR(64)     NOT NULL,
    s3_key       VARCHAR(300) NULL,
    ingested_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (batch_id, file_name)
);

-- One audit row per pipeline run.
CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id          CHAR(36)    NOT NULL PRIMARY KEY,
    batch_id        VARCHAR(10) NOT NULL,
    started_at      DATETIME    NOT NULL,
    finished_at     DATETIME    NULL,
    status          VARCHAR(10) NOT NULL,              -- RUNNING / SUCCESS / FAILED
    rows_read       INT         NOT NULL DEFAULT 0,
    rows_loaded     INT         NOT NULL DEFAULT 0,
    rows_rejected   INT         NOT NULL DEFAULT 0,
    rows_duplicate  INT         NOT NULL DEFAULT 0,
    rows_corrected  INT         NOT NULL DEFAULT 0,
    error_message   TEXT        NULL,
    KEY ix_runs_batch (batch_id, started_at)
);

-- Every rejected record, with the original raw values and all reasons.
CREATE TABLE IF NOT EXISTS dq_quarantine (
    quarantine_id   INT AUTO_INCREMENT PRIMARY KEY,
    run_id          CHAR(36)     NOT NULL,
    batch_id        VARCHAR(10)  NOT NULL,
    entity          VARCHAR(20)  NOT NULL,
    source_file     VARCHAR(100) NOT NULL,
    record_key      VARCHAR(50)  NULL,                 -- e.g. the transaction_id
    reason_codes    VARCHAR(300) NOT NULL,             -- e.g. 'ORPHAN_ACCOUNT;INVALID_AMOUNT'
    raw_record      TEXT         NOT NULL,             -- exactly as received
    quarantined_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY ix_quarantine_batch (batch_id, entity)
);

-- Data-quality scorecard per source file per batch (KPI 10).
CREATE TABLE IF NOT EXISTS dq_scorecard (
    batch_id            VARCHAR(10)  NOT NULL,
    source_file         VARCHAR(100) NOT NULL,
    entity              VARCHAR(20)  NOT NULL,
    records_received    INT          NOT NULL,
    records_passed      INT          NOT NULL,
    records_rejected    INT          NOT NULL,
    duplicates_removed  INT          NOT NULL,
    run_id              CHAR(36)     NOT NULL,
    PRIMARY KEY (batch_id, source_file),
    CONSTRAINT ck_scorecard_balances
        CHECK (records_received = records_passed + records_rejected + duplicates_removed)
);

-- Count of each issue per file: REJECT = quarantined, FLAG = fixed/flagged but kept.
CREATE TABLE IF NOT EXISTS dq_issue_counts (
    batch_id      VARCHAR(10)  NOT NULL,
    source_file   VARCHAR(100) NOT NULL,
    severity      VARCHAR(6)   NOT NULL,               -- REJECT / FLAG
    reason_code   VARCHAR(40)  NOT NULL,
    record_count  INT          NOT NULL,
    PRIMARY KEY (batch_id, source_file, severity, reason_code)
);

-- KPI results saved per batch so Day 2 can be compared with Day 1 (KPI 11).
CREATE TABLE IF NOT EXISTS kpi_snapshot (
    batch_id      VARCHAR(10)   NOT NULL,
    kpi_name      VARCHAR(40)   NOT NULL,
    rank_no       INT           NOT NULL,
    item_key      VARCHAR(50)   NOT NULL,
    item_label    VARCHAR(100)  NULL,
    metric_value  DECIMAL(18,2) NOT NULL,
    PRIMARY KEY (batch_id, kpi_name, rank_no)
);
