-- =============================================================================
-- Migration 0003: Phase 3 — Helpdesk / Ticket System
--
-- Purpose:
--   Introduces the ticket system as four brand-new tables. Does NOT touch
--   any Phase 1 or Phase 2 table (admins, employees, assets, assignments).
--
-- Guarantees:
--   - Does NOT drop or recreate any existing table.
--   - Does NOT modify any existing column on any existing table.
--   - Safe to re-run: every CREATE TABLE uses IF NOT EXISTS, which is
--     natively idempotent in MySQL/MariaDB (no stored-procedure trick
--     needed here, unlike the column-level migrations 0001/0002).
--
-- Compatible with MySQL 5.7+/8.x and MariaDB 10.x.
-- =============================================================================

CREATE TABLE IF NOT EXISTS tickets (
  id                        INT PRIMARY KEY AUTO_INCREMENT,
  ticket_number             VARCHAR(20)  NOT NULL UNIQUE,
  employee_id               INT NOT NULL,
  asset_id                  INT NOT NULL,
  category                  VARCHAR(30)  NOT NULL,
  priority                  VARCHAR(20)  NOT NULL,
  description               TEXT NOT NULL,
  status                    VARCHAR(20)  NOT NULL DEFAULT 'Open',
  assigned_admin_id         INT NULL,
  expected_resolution_date  DATE NULL,
  resolution_notes          TEXT NULL,
  resolved_at               DATETIME NULL,
  closed_at                 DATETIME NULL,
  reopen_count              INT NOT NULL DEFAULT 0,
  created_at                DATETIME NOT NULL,
  updated_at                DATETIME NOT NULL,
  CONSTRAINT fk_tickets_employee FOREIGN KEY (employee_id) REFERENCES employees(id),
  CONSTRAINT fk_tickets_asset    FOREIGN KEY (asset_id)    REFERENCES assets(id),
  CONSTRAINT fk_tickets_admin    FOREIGN KEY (assigned_admin_id) REFERENCES admins(id) ON DELETE SET NULL,
  INDEX idx_tickets_employee (employee_id),
  INDEX idx_tickets_asset (asset_id),
  INDEX idx_tickets_status (status)
);

CREATE TABLE IF NOT EXISTS ticket_messages (
  id            INT PRIMARY KEY AUTO_INCREMENT,
  ticket_id     INT NOT NULL,
  sender_role   VARCHAR(20) NOT NULL,
  employee_id   INT NULL,
  admin_id      INT NULL,
  message       TEXT NOT NULL,
  created_at    DATETIME NOT NULL,
  CONSTRAINT fk_ticket_messages_ticket   FOREIGN KEY (ticket_id)   REFERENCES tickets(id) ON DELETE CASCADE,
  CONSTRAINT fk_ticket_messages_employee FOREIGN KEY (employee_id) REFERENCES employees(id),
  CONSTRAINT fk_ticket_messages_admin    FOREIGN KEY (admin_id)    REFERENCES admins(id),
  INDEX idx_ticket_messages_ticket (ticket_id)
);

-- Approved refinement: event_type distinguishes status transitions from
-- non-status events (assignment, expected-date changes) so the timeline
-- stays semantically clean; old_status/new_status remain nullable and are
-- only populated for actual status-changing events.
CREATE TABLE IF NOT EXISTS ticket_status_history (
  id                        INT PRIMARY KEY AUTO_INCREMENT,
  ticket_id                 INT NOT NULL,
  event_type                VARCHAR(30) NOT NULL,
  old_status                VARCHAR(20) NULL,
  new_status                VARCHAR(20) NULL,
  changed_by_role           VARCHAR(20) NOT NULL,
  changed_by_admin_id       INT NULL,
  changed_by_employee_id    INT NULL,
  note                      TEXT NULL,
  created_at                DATETIME NOT NULL,
  CONSTRAINT fk_ticket_history_ticket   FOREIGN KEY (ticket_id) REFERENCES tickets(id) ON DELETE CASCADE,
  CONSTRAINT fk_ticket_history_admin    FOREIGN KEY (changed_by_admin_id)    REFERENCES admins(id),
  CONSTRAINT fk_ticket_history_employee FOREIGN KEY (changed_by_employee_id) REFERENCES employees(id),
  INDEX idx_ticket_history_ticket (ticket_id)
);

-- Approved refinement: message_id links an attachment to the specific reply
-- it was sent with; NULL means it was attached at initial ticket creation.
CREATE TABLE IF NOT EXISTS ticket_attachments (
  id                        INT PRIMARY KEY AUTO_INCREMENT,
  ticket_id                 INT NOT NULL,
  message_id                INT NULL,
  uploaded_by_role          VARCHAR(20) NOT NULL,
  uploaded_by_employee_id   INT NULL,
  uploaded_by_admin_id      INT NULL,
  original_filename         VARCHAR(255) NOT NULL,
  stored_filename           VARCHAR(255) NOT NULL,
  content_type              VARCHAR(100) NOT NULL,
  file_size_bytes           INT NOT NULL,
  created_at                DATETIME NOT NULL,
  CONSTRAINT fk_ticket_attachments_ticket   FOREIGN KEY (ticket_id)  REFERENCES tickets(id) ON DELETE CASCADE,
  CONSTRAINT fk_ticket_attachments_message  FOREIGN KEY (message_id) REFERENCES ticket_messages(id) ON DELETE CASCADE,
  CONSTRAINT fk_ticket_attachments_employee FOREIGN KEY (uploaded_by_employee_id) REFERENCES employees(id),
  CONSTRAINT fk_ticket_attachments_admin    FOREIGN KEY (uploaded_by_admin_id)    REFERENCES admins(id),
  INDEX idx_ticket_attachments_ticket (ticket_id),
  INDEX idx_ticket_attachments_message (message_id)
);

-- =============================================================================
-- Note on idempotency and foreign keys:
--   CREATE TABLE IF NOT EXISTS is a true no-op on a table that already has
--   the exact shape below. If you need to add a constraint to a
--   Phase-3-created table that's missing one (e.g. after a partial manual
--   apply), do so by hand — this file intentionally does not attempt
--   ALTER-based FK backfilling for brand-new tables, since db.create_all()
--   on the Flask side always creates them with the full shape in one shot.
--
-- Rollback (manual, if ever needed — NOT run automatically):
--
--   DROP TABLE IF EXISTS ticket_attachments;
--   DROP TABLE IF EXISTS ticket_status_history;
--   DROP TABLE IF EXISTS ticket_messages;
--   DROP TABLE IF EXISTS tickets;
--
-- Back up your database before running any rollback — this permanently
-- deletes all tickets, messages, history, and attachment records.
-- =============================================================================
