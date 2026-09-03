-- Phase 5: warranty tracking for assets.
ALTER TABLE assets ADD COLUMN IF NOT EXISTS warranty_expiry DATE NULL;
ALTER TABLE assets ADD COLUMN IF NOT EXISTS warranty_provider VARCHAR(160) NULL;
