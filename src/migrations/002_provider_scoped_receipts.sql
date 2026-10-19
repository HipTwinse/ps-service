-- Migration 002: Add provider_name to payment_receipts and scope provider_tx_id uniqueness by provider

ALTER TABLE payment_receipts ADD COLUMN provider_name TEXT NOT NULL DEFAULT 'LEGACY_UNSPECIFIED';

DROP INDEX IF EXISTS idx_receipts_provider_tx;

CREATE UNIQUE INDEX IF NOT EXISTS idx_receipts_provider_tx 
ON payment_receipts(provider_name, provider_tx_id) 
WHERE provider_tx_id IS NOT NULL;
