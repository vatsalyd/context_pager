-- Add tenant_id column to users table (missed in 001 schema).
ALTER TABLE users ADD COLUMN IF NOT EXISTS tenant_id text NOT NULL DEFAULT '';
-- Update existing rows (all have empty tenant_id from Phase A testing)
UPDATE users SET tenant_id = 'tenant_' || replace(id::text, '-', '') WHERE tenant_id = '';
-- Make tenant_id NOT NULL after defaults are populated
ALTER TABLE users ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE users ALTER COLUMN tenant_id DROP DEFAULT;