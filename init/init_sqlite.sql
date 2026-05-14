-- ──────────────────────────────────────────────
--  SQLite init script for Personal Assistant
--  Creates tables for tracking emails, payments, and documents.
-- ──────────────────────────────────────────────

-- Table to track emails that have already been read and processed
CREATE TABLE IF NOT EXISTS processed_emails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT UNIQUE NOT NULL,
    thread_id TEXT,
    subject TEXT,
    sender_email TEXT,
    recipient_email TEXT,
    snippet TEXT,
    mime_type TEXT,
    size_estimate INTEGER,
    history_id TEXT,
    internal_date INTEGER,
    labels TEXT,
    category TEXT,
    received_at DATETIME,
    processed_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Table to record extracted payments
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    processed_email_id INTEGER REFERENCES processed_emails(id) ON DELETE SET NULL,
    payer_name TEXT,
    amount REAL,
    currency TEXT,
    payment_date DATETIME,
    reference_note TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- ──────────────────────────────────────────────
--  Document Index
-- ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    filepath TEXT,
    section TEXT NOT NULL DEFAULT 'personal',
    category TEXT,
    description TEXT,
    tags TEXT DEFAULT '[]',
    status TEXT DEFAULT 'complete',
    status_note TEXT,
    stored_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
