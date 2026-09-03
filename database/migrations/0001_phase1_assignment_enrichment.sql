-- =============================================================================
-- Migration 0001: Phase 1 — Asset <-> Employee Assignment Enrichment
--
-- Purpose:
--   Adds condition/expected-return/remarks/assigned-by fields needed to
--   properly display and audit asset assignments, as an ADDITIVE extension
--   of the existing `assets` and `assignments` tables.
--
-- Guarantees:
--   - Does NOT drop or recreate any existing table.
--   - Does NOT modify or remove any existing column.
--   - Does NOT touch existing row data — all new columns are nullable and
--     are left NULL on existing rows (no fabricated history).
--   - Safe to re-run: every ALTER is guarded by an information_schema check,
--     so running this twice against the same database is a no-op the
--     second time.
--
-- Compatible with MySQL 5.7+/8.x and MariaDB 10.x (uses a small stored
-- procedure to emulate "ADD COLUMN IF NOT EXISTS", which older MySQL/MariaDB
-- versions do not support natively).
-- =============================================================================

DELIMITER $$

DROP PROCEDURE IF EXISTS _phase1_add_column $$
CREATE PROCEDURE _phase1_add_column(
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

-- --- assets: current-state condition field --------------------------------
CALL _phase1_add_column('assets', 'condition_status', 'condition_status VARCHAR(20) NULL');

-- --- assignments: per-event enrichment fields ------------------------------
CALL _phase1_add_column('assignments', 'expected_return_date',    'expected_return_date DATE NULL');
CALL _phase1_add_column('assignments', 'condition_at_assignment', 'condition_at_assignment VARCHAR(20) NULL');
CALL _phase1_add_column('assignments', 'remarks',                 'remarks TEXT NULL');
CALL _phase1_add_column('assignments', 'assigned_by_admin_id',    'assigned_by_admin_id INT NULL');

-- --- assignments: foreign key to admins (who performed the action) --------
-- Guarded separately: constraint names, not column names, must be checked here.
DELIMITER $$

DROP PROCEDURE IF EXISTS _phase1_add_fk $$
CREATE PROCEDURE _phase1_add_fk()
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE()
      AND TABLE_NAME = 'assignments'
      AND CONSTRAINT_NAME = 'fk_assignments_admin'
  ) THEN
    ALTER TABLE assignments
      ADD CONSTRAINT fk_assignments_admin
      FOREIGN KEY (assigned_by_admin_id) REFERENCES admins(id) ON DELETE SET NULL;
  END IF;
END $$

DELIMITER ;

CALL _phase1_add_fk();

-- --- cleanup: drop the helper procedures, they're only needed at migration time
DROP PROCEDURE IF EXISTS _phase1_add_column;
DROP PROCEDURE IF EXISTS _phase1_add_fk;

-- =============================================================================
-- Rollback (manual, if ever needed — NOT run automatically):
--
--   ALTER TABLE assignments DROP FOREIGN KEY fk_assignments_admin;
--   ALTER TABLE assignments DROP COLUMN assigned_by_admin_id;
--   ALTER TABLE assignments DROP COLUMN remarks;
--   ALTER TABLE assignments DROP COLUMN condition_at_assignment;
--   ALTER TABLE assignments DROP COLUMN expected_return_date;
--   ALTER TABLE assets      DROP COLUMN condition_status;
--
-- Back up your database before running any rollback — dropping a column
-- permanently deletes the data in it.
-- =============================================================================
