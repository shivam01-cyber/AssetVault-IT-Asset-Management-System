-- =============================================================================
-- Migration 0002: Phase 2 — Employee Authentication & Role-Based Access
--
-- Purpose:
--   Adds login credentials and account status to the existing `employees`
--   table so employees can authenticate, without touching admin auth at all.
--
-- Guarantees:
--   - Does NOT drop or recreate the `employees` table.
--   - Does NOT modify or remove any existing employee column
--     (id, name, email, department, job_title, created_at all untouched).
--   - Does NOT touch existing row data beyond the two new columns below.
--   - `password_hash` is nullable and is NEVER auto-populated — existing
--     employees keep password_hash = NULL until an admin explicitly sets one.
--   - `account_status` defaults to 'Inactive', so no existing employee can
--     log in as a side effect of this migration, even after a password is
--     eventually set — activation is a separate, explicit admin action.
--   - Safe to re-run: every ALTER is guarded by an information_schema check.
--
-- Compatible with MySQL 5.7+/8.x and MariaDB 10.x.
-- =============================================================================

DELIMITER $$

DROP PROCEDURE IF EXISTS _phase2_add_column $$
CREATE PROCEDURE _phase2_add_column(
  IN p_table   VARCHAR(64),
  IN p_column  VARCHAR(64),
  IN p_ddl     VARCHAR(255)
)
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = p_table
      AND COLUMN_NAME = p_column
  ) THEN
    SET @ddl = CONCAT('ALTER TABLE ', p_table, ' ADD COLUMN ', p_ddl);
    PREPARE stmt FROM @ddl;
    EXECUTE stmt;
    DEALLOCATE PREPARE stmt;
  END IF;
END $$

DELIMITER ;

-- --- employees: login credentials + account status -------------------------
CALL _phase2_add_column('employees', 'password_hash', 'password_hash VARCHAR(255) NULL');
CALL _phase2_add_column('employees', 'account_status', "account_status VARCHAR(20) NOT NULL DEFAULT 'Inactive'");

-- --- cleanup: drop the helper procedure, it's only needed at migration time
DROP PROCEDURE IF EXISTS _phase2_add_column;

-- =============================================================================
-- Post-migration verification (optional, read-only):
--
--   SELECT id, name, email,
--          CASE WHEN password_hash IS NULL THEN 'Login Not Configured'
--               ELSE account_status END AS login_state
--   FROM employees;
--
-- Every pre-existing employee should show 'Login Not Configured' until an
-- admin sets a password for them via the admin Employees screen.
--
-- Rollback (manual, if ever needed — NOT run automatically):
--
--   ALTER TABLE employees DROP COLUMN account_status;
--   ALTER TABLE employees DROP COLUMN password_hash;
--
-- Back up your database before running any rollback — dropping a column
-- permanently deletes the data in it (in this case, all configured
-- employee login credentials).
-- =============================================================================
