-- ============================================================
-- OloidBot — Supabase Schema
-- Run this ONCE in: Supabase Dashboard → SQL Editor → New Query
-- ============================================================

-- 1. Admin configuration (singleton row, id always = 1)
CREATE TABLE admin_config (
    id INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    google_client_id TEXT NOT NULL DEFAULT '',
    google_client_secret TEXT NOT NULL DEFAULT '',
    llm_provider TEXT NOT NULL DEFAULT '',
    llm_api_key TEXT NOT NULL DEFAULT '',
    llm_model TEXT NOT NULL DEFAULT '',
    memory_channel_id TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Seed the singleton row so UPDATE always has a target
INSERT INTO admin_config (id) VALUES (1);

-- 2. Per-user data
CREATE TABLE users (
    slack_user_id TEXT PRIMARY KEY,
    gmail_token TEXT NOT NULL DEFAULT '',
    llm_provider TEXT NOT NULL DEFAULT '',
    llm_api_key TEXT NOT NULL DEFAULT '',
    llm_model TEXT NOT NULL DEFAULT '',
    notifications BOOLEAN NOT NULL DEFAULT TRUE,
    last_poll_ts DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Index for the scheduler's "all connected users" query
CREATE INDEX idx_users_gmail_connected
    ON users (slack_user_id)
    WHERE gmail_token != '';

-- 3. Reminders (one-to-many with users)
CREATE TABLE reminders (
    id TEXT PRIMARY KEY,
    slack_user_id TEXT NOT NULL REFERENCES users(slack_user_id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    fire_at DOUBLE PRECISION NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Index for "get all due reminders" (scheduler scans by fire_at)
CREATE INDEX idx_reminders_fire_at ON reminders (fire_at);

-- Index for "get reminders for a user"
CREATE INDEX idx_reminders_user ON reminders (slack_user_id);

-- 4. OAuth pending states (short-lived, consumed after callback)
CREATE TABLE oauth_pending (
    state TEXT PRIMARY KEY,
    slack_user_id TEXT NOT NULL,
    code_verifier TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================
-- Auto-update updated_at on row changes
-- ============================================================

CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_admin_config_updated_at
    BEFORE UPDATE ON admin_config
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER trg_users_updated_at
    BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- ============================================================
-- Auto-cleanup: delete OAuth states older than 15 minutes
-- Uses pg_cron (available on Supabase free tier)
-- ============================================================

-- Enable pg_cron if not already enabled (run once)
CREATE EXTENSION IF NOT EXISTS pg_cron;

-- Schedule cleanup every 5 minutes
SELECT cron.schedule(
    'cleanup-expired-oauth-states',
    '*/5 * * * *',
    $$DELETE FROM oauth_pending WHERE created_at < NOW() - INTERVAL '15 minutes'$$
);

-- ============================================================
-- Row Level Security (RLS)
-- Supabase enables RLS by default on new tables. These policies
-- allow the service role / anon key (used by the bot) full access
-- while blocking direct access from other clients.
-- ============================================================

-- Enable RLS on all tables
ALTER TABLE admin_config ENABLE ROW LEVEL SECURITY;
ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE reminders ENABLE ROW LEVEL SECURITY;
ALTER TABLE oauth_pending ENABLE ROW LEVEL SECURITY;

-- Allow the service role (or anon key via API) full CRUD.
-- The bot is the ONLY client — no end-user direct DB access.
CREATE POLICY "service_full_access" ON admin_config
    FOR ALL USING (true) WITH CHECK (true);

CREATE POLICY "service_full_access" ON users
    FOR ALL USING (true) WITH CHECK (true);

CREATE POLICY "service_full_access" ON reminders
    FOR ALL USING (true) WITH CHECK (true);

CREATE POLICY "service_full_access" ON oauth_pending
    FOR ALL USING (true) WITH CHECK (true);

-- ============================================================
-- Connection pooling note
-- ============================================================
-- Supabase free tier includes a connection pooler (PgBouncer)
-- accessible via port 6543. The Python SDK uses the REST API
-- (PostgREST) which handles pooling server-side, so no extra
-- config is needed. The httpx pool in user_store.py handles
-- the HTTP connection reuse on the client side.
-- ============================================================
