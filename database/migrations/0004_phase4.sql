-- =============================================================================
-- Migration 0004: Phase 4 — Notifications & Employee Profile Photos
-- Compatible with MySQL 5.7+/8.x and MariaDB 10.x.
-- Additive and idempotent: no existing table is dropped or recreated.
-- =============================================================================

DELIMITER $$

DROP PROCEDURE IF EXISTS _phase4_add_column $$
CREATE PROCEDURE _phase4_add_column(
  IN p_table VARCHAR(64), IN p_column VARCHAR(64), IN p_ddl VARCHAR(255)
)
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = p_table AND COLUMN_NAME = p_column
  ) THEN
    SET @ddl = CONCAT('ALTER TABLE ', p_table, ' ADD COLUMN ', p_ddl);
    PREPARE stmt FROM @ddl;
    EXECUTE stmt;
    DEALLOCATE PREPARE stmt;
  END IF;
END $$

DELIMITER ;

CALL _phase4_add_column('employees', 'profile_image_filename', 'profile_image_filename VARCHAR(255) NULL');
CALL _phase4_add_column('employees', 'profile_image_content_type', 'profile_image_content_type VARCHAR(100) NULL');
DROP PROCEDURE IF EXISTS _phase4_add_column;

CREATE TABLE IF NOT EXISTS notifications (
  id INT PRIMARY KEY AUTO_INCREMENT,
  employee_id INT NULL,
  admin_id INT NULL,
  title VARCHAR(160) NOT NULL,
  message TEXT NOT NULL,
  link_url VARCHAR(255) NULL,
  is_read BOOLEAN NOT NULL DEFAULT FALSE,
  created_at DATETIME NOT NULL,
  INDEX idx_notifications_employee (employee_id),
  INDEX idx_notifications_admin (admin_id),
  INDEX idx_notifications_read (is_read),
  INDEX idx_notifications_created (created_at),
  CONSTRAINT fk_notifications_employee FOREIGN KEY (employee_id) REFERENCES employees(id) ON DELETE CASCADE,
  CONSTRAINT fk_notifications_admin FOREIGN KEY (admin_id) REFERENCES admins(id) ON DELETE CASCADE
);
