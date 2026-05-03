-- ──────────────────────────────────────────────
--  PostgreSQL init script for n8n
--  Runs once when the postgres_data volume is first created.
--  Mounted at /docker-entrypoint-initdb.d/init.sql
-- ──────────────────────────────────────────────

-- Ensure the n8n database exists (created by POSTGRES_DB env var,
-- but this is a safety net).
SELECT 'n8n_db database ready' AS status;

-- Grant full privileges to the n8n user on the public schema
GRANT ALL PRIVILEGES ON DATABASE n8n_db TO n8n_user;
GRANT ALL PRIVILEGES ON SCHEMA public TO n8n_user;

-- Optional: create an extension n8n may use for UUID generation
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ──────────────────────────────────────────────
--  Custom Application Tables
-- ──────────────────────────────────────────────

-- Table to track emails that have already been read and processed
CREATE TABLE IF NOT EXISTS processed_emails (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    message_id VARCHAR(255) UNIQUE NOT NULL,
    thread_id VARCHAR(255),
    subject VARCHAR(512),
    sender_email VARCHAR(255),
    recipient_email VARCHAR(255),
    snippet TEXT,
    mime_type VARCHAR(100),
    size_estimate INT,
    history_id VARCHAR(255),
    internal_date BIGINT,
    labels JSONB,
    -- Categories extracted by the Hermes agent:
    --   'payment_receipt'  : Official proof of payment
    --   'invoice_received' : An invoice that needs to be paid
    --   'payment_failed'   : Declined transaction alert
    --   'statement'        : Monthly bank summary
    --   'inquiry'          : Client asking about payment status
    --   'unrelated'        : Spam or non-actionable email
    category VARCHAR(50),
    received_at TIMESTAMP WITH TIME ZONE,
    processed_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Table to record extracted payments
CREATE TABLE IF NOT EXISTS payments (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    processed_email_id UUID REFERENCES processed_emails(id) ON DELETE SET NULL,
    payer_name VARCHAR(255),
    amount NUMERIC(15, 2),
    currency VARCHAR(10),
    payment_date TIMESTAMP WITH TIME ZONE,
    reference_note TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- ──────────────────────────────────────────────
--  Document Index (replaces index.md)
-- ──────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS documents (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    filename VARCHAR(512) NOT NULL,
    filepath VARCHAR(1024),
    -- Top-level: 'personal', 'work', 'archive'
    section VARCHAR(50) NOT NULL DEFAULT 'personal',
    -- Sub-category: 'health', 'finance', 'home', 'legal', 'projects', 'photos', 'misc', 'reports'
    category VARCHAR(50),
    description TEXT,
    tags JSONB DEFAULT '[]',
    -- 'complete', 'pending', 'in_progress'
    status VARCHAR(20) DEFAULT 'complete',
    status_note TEXT,
    stored_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Example: seed your own documents
-- INSERT INTO documents (filename, filepath, section, category, description, tags, status, status_note, stored_at) VALUES
-- ('example_document.pdf', 'Personal/Finance/', 'personal', 'finance',
--  'Example bank statement', '["bank", "statement"]', 'complete', NULL, '2025-01-01')
-- ON CONFLICT DO NOTHING;
