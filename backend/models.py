"""SQLAlchemy models for the IT & Digital Asset Management System."""
from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

ASSET_CATEGORIES = [
    "Laptop", "Desktop", "Monitor", "Printer", "Mobile Phone",
    "Tablet", "Server", "Network Device", "Software License", "Accessory",
]
ASSET_STATUSES = ["Available", "Assigned", "Under Repair", "Retired"]

# Phase 1: condition vocabulary shared by Asset.condition_status (current condition)
# and Assignment.condition_at_assignment (condition recorded at a specific event).
ASSET_CONDITIONS = ["Working", "Good", "Fair", "Damaged", "Not Working"]

# Phase 2: employee account status vocabulary.
EMPLOYEE_ACCOUNT_STATUSES = ["Active", "Inactive"]

# Phase 3: helpdesk/ticket vocabulary.
TICKET_CATEGORIES = ["Hardware", "Software", "Network", "Performance", "Physical Damage", "Other"]
TICKET_PRIORITIES = ["Low", "Medium", "High", "Critical"]
TICKET_STATUSES = ["Open", "Acknowledged", "In Progress", "Waiting For Part", "Resolved", "Closed", "Reopened"]

# Statuses an employee is permitted to move a ticket into (server-enforced; see transition_ticket_status()).
EMPLOYEE_ALLOWED_TRANSITIONS = {("Resolved", "Reopened")}

TICKET_EVENT_TYPES = [
    "STATUS_CHANGED", "EXPECTED_DATE_CHANGED", "ASSIGNED", "RESOLVED", "CLOSED", "REOPENED",
]

TICKET_ATTACHMENT_EXTENSIONS = {"jpg", "jpeg", "png", "pdf"}
TICKET_ATTACHMENT_MAX_BYTES = 5 * 1024 * 1024  # 5 MB


def _utcnow():
    return datetime.now(timezone.utc)


class Admin(db.Model):
    """Application administrator (single seeded account by default)."""
    __tablename__ = "admins"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(191), unique=True, nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False, default="Administrator")
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=_utcnow)


class Employee(db.Model):
    __tablename__ = "employees"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(191), unique=True, nullable=False)
    department = db.Column(db.String(120), nullable=False, default="General")
    job_title = db.Column(db.String(120), nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow)

    # --- Phase 2 additions: employee login. Nullable/defaulted so existing rows
    # stay valid and stay logged-out until an admin explicitly configures them. ---
    password_hash = db.Column(db.String(255), nullable=True)
    account_status = db.Column(db.String(20), nullable=False, default="Inactive")
    profile_image_filename = db.Column(db.String(255), nullable=True)
    profile_image_content_type = db.Column(db.String(100), nullable=True)

    assets = db.relationship("Asset", back_populates="employee")

    @property
    def asset_count(self):
        return len([a for a in self.assets if a.status == "Assigned"])

    @property
    def has_login_configured(self):
        """False until an admin has set a password for this employee."""
        return self.password_hash is not None

    @property
    def login_state(self):
        """Human-readable login/account state for admin-facing UI.

        Distinct from account_status: an employee can be 'Active' in the
        database default sense but still have no password set, in which
        case login is not actually possible yet.
        """
        if not self.has_login_configured:
            return "Login Not Configured"
        return self.account_status

    def to_dict(self):
        # Deliberately never includes password_hash.
        return {
            "id": self.id,
            "name": self.name,
            "email": self.email,
            "department": self.department,
            "job_title": self.job_title or "",
            "asset_count": self.asset_count,
            "account_status": self.account_status,
            "has_login_configured": self.has_login_configured,
            "login_state": self.login_state,
            "profile_image_filename": self.profile_image_filename or "",
        }


class Asset(db.Model):
    __tablename__ = "assets"

    id = db.Column(db.Integer, primary_key=True)
    serial_number = db.Column(db.String(120), unique=True, nullable=False)
    device_name = db.Column(db.String(160), nullable=False)
    category = db.Column(db.String(60), nullable=False, default="Laptop")
    status = db.Column(db.String(40), nullable=False, default="Available")
    purchase_date = db.Column(db.Date, nullable=True)
    notes = db.Column(db.Text, nullable=True)
    assigned_date = db.Column(db.Date, nullable=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("employees.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow)

    # Phase 1 addition: current condition of the asset. Nullable so existing
    # rows are unaffected; distinct from Assignment.condition_at_assignment,
    # which records condition at a specific historical event.
    condition_status = db.Column(db.String(20), nullable=True)

    # Phase 5: warranty tracking. Nullable so all existing assets remain valid.
    warranty_expiry = db.Column(db.Date, nullable=True)
    warranty_provider = db.Column(db.String(160), nullable=True)

    employee = db.relationship("Employee", back_populates="assets")

    def to_dict(self):
        return {
            "id": self.id,
            "serial_number": self.serial_number,
            "device_name": self.device_name,
            "category": self.category,
            "status": self.status,
            "purchase_date": self.purchase_date.isoformat() if self.purchase_date else "",
            "assigned_date": self.assigned_date.isoformat() if self.assigned_date else "",
            "notes": self.notes or "",
            "employee_id": self.employee_id,
            "employee_name": self.employee.name if self.employee else "",
            "condition_status": self.condition_status or "",
            "warranty_expiry": self.warranty_expiry.isoformat() if self.warranty_expiry else "",
            "warranty_provider": self.warranty_provider or "",
        }


class Assignment(db.Model):
    """Immutable audit log of every assign / return action."""
    __tablename__ = "assignments"

    id = db.Column(db.Integer, primary_key=True)
    asset_id = db.Column(db.Integer, db.ForeignKey("assets.id"), nullable=False)
    employee_id = db.Column(db.Integer, db.ForeignKey("employees.id"), nullable=False)
    action = db.Column(db.String(20), nullable=False, default="assigned")
    action_date = db.Column(db.Date, nullable=False)
    created_at = db.Column(db.DateTime, default=_utcnow)

    # --- Phase 1 additions: all nullable so historical rows stay valid as-is. ---
    expected_return_date = db.Column(db.Date, nullable=True)
    condition_at_assignment = db.Column(db.String(20), nullable=True)
    remarks = db.Column(db.Text, nullable=True)
    assigned_by_admin_id = db.Column(db.Integer, db.ForeignKey("admins.id"), nullable=True)

    asset = db.relationship("Asset")
    employee = db.relationship("Employee")
    assigned_by = db.relationship("Admin")

    def to_dict(self):
        return {
            "id": self.id,
            "asset_id": self.asset_id,
            "asset_name": self.asset.device_name if self.asset else "Deleted asset",
            "employee_id": self.employee_id,
            "employee_name": self.employee.name if self.employee else "—",
            "action": self.action,
            "action_date": self.action_date.isoformat() if self.action_date else "",
            "expected_return_date": self.expected_return_date.isoformat() if self.expected_return_date else "",
            "condition_at_assignment": self.condition_at_assignment or "",
            "remarks": self.remarks or "",
            "assigned_by_name": self.assigned_by.name if self.assigned_by else "",
            "created_at": self.created_at.isoformat() if self.created_at else "",
        }


# --------------------------------------------------------------------------- #
# Phase 3: Helpdesk / Ticket system
# --------------------------------------------------------------------------- #
class Ticket(db.Model):
    """A single reported asset issue and its lifecycle."""
    __tablename__ = "tickets"

    id = db.Column(db.Integer, primary_key=True)
    ticket_number = db.Column(db.String(20), unique=True, nullable=False)  # e.g. TKT-2026-00001
    employee_id = db.Column(db.Integer, db.ForeignKey("employees.id"), nullable=False)
    asset_id = db.Column(db.Integer, db.ForeignKey("assets.id"), nullable=False)
    category = db.Column(db.String(30), nullable=False)
    priority = db.Column(db.String(20), nullable=False)
    description = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), nullable=False, default="Open")
    assigned_admin_id = db.Column(db.Integer, db.ForeignKey("admins.id"), nullable=True)
    expected_resolution_date = db.Column(db.Date, nullable=True)
    resolution_notes = db.Column(db.Text, nullable=True)
    resolved_at = db.Column(db.DateTime, nullable=True)
    closed_at = db.Column(db.DateTime, nullable=True)
    reopen_count = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=_utcnow)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)

    employee = db.relationship("Employee")
    asset = db.relationship("Asset")
    assigned_admin = db.relationship("Admin")
    messages = db.relationship(
        "TicketMessage", back_populates="ticket", order_by="TicketMessage.created_at", cascade="all, delete-orphan"
    )
    history = db.relationship(
        "TicketStatusHistory", back_populates="ticket", order_by="TicketStatusHistory.created_at",
        cascade="all, delete-orphan",
    )
    attachments = db.relationship(
        "TicketAttachment", back_populates="ticket", order_by="TicketAttachment.created_at",
        cascade="all, delete-orphan",
    )

    def to_dict(self):
        return {
            "id": self.id,
            "ticket_number": self.ticket_number,
            "employee_id": self.employee_id,
            "employee_name": self.employee.name if self.employee else "—",
            "asset_id": self.asset_id,
            "asset_name": self.asset.device_name if self.asset else "Deleted asset",
            "category": self.category,
            "priority": self.priority,
            "description": self.description,
            "status": self.status,
            "assigned_admin_name": self.assigned_admin.name if self.assigned_admin else "",
            "expected_resolution_date": self.expected_resolution_date.isoformat() if self.expected_resolution_date else "",
            "resolution_notes": self.resolution_notes or "",
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else "",
            "closed_at": self.closed_at.isoformat() if self.closed_at else "",
            "reopen_count": self.reopen_count,
            "created_at": self.created_at.isoformat() if self.created_at else "",
            "updated_at": self.updated_at.isoformat() if self.updated_at else "",
        }


class TicketMessage(db.Model):
    """A single message in a ticket's conversation thread."""
    __tablename__ = "ticket_messages"

    id = db.Column(db.Integer, primary_key=True)
    ticket_id = db.Column(db.Integer, db.ForeignKey("tickets.id"), nullable=False)
    sender_role = db.Column(db.String(20), nullable=False)  # 'employee' | 'admin'
    employee_id = db.Column(db.Integer, db.ForeignKey("employees.id"), nullable=True)
    admin_id = db.Column(db.Integer, db.ForeignKey("admins.id"), nullable=True)
    message = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=_utcnow)

    ticket = db.relationship("Ticket", back_populates="messages")
    employee = db.relationship("Employee")
    admin = db.relationship("Admin")
    attachments = db.relationship("TicketAttachment", back_populates="message")

    @property
    def sender_name(self):
        if self.sender_role == "employee":
            return self.employee.name if self.employee else "Former employee"
        return self.admin.name if self.admin else "IT Support"

    def to_dict(self):
        return {
            "id": self.id,
            "ticket_id": self.ticket_id,
            "sender_role": self.sender_role,
            "sender_name": self.sender_name,
            "message": self.message,
            "created_at": self.created_at.isoformat() if self.created_at else "",
        }


class TicketStatusHistory(db.Model):
    """Immutable, append-only timeline of everything that happened to a ticket."""
    __tablename__ = "ticket_status_history"

    id = db.Column(db.Integer, primary_key=True)
    ticket_id = db.Column(db.Integer, db.ForeignKey("tickets.id"), nullable=False)
    event_type = db.Column(db.String(30), nullable=False)
    old_status = db.Column(db.String(20), nullable=True)
    new_status = db.Column(db.String(20), nullable=True)
    changed_by_role = db.Column(db.String(20), nullable=False)  # 'employee' | 'admin' | 'system'
    changed_by_admin_id = db.Column(db.Integer, db.ForeignKey("admins.id"), nullable=True)
    changed_by_employee_id = db.Column(db.Integer, db.ForeignKey("employees.id"), nullable=True)
    note = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow)

    ticket = db.relationship("Ticket", back_populates="history")
    changed_by_admin = db.relationship("Admin")
    changed_by_employee = db.relationship("Employee")

    @property
    def actor_name(self):
        if self.changed_by_role == "admin":
            return self.changed_by_admin.name if self.changed_by_admin else "IT Support"
        if self.changed_by_role == "employee":
            return self.changed_by_employee.name if self.changed_by_employee else "Employee"
        return "System"

    def to_dict(self):
        return {
            "id": self.id,
            "ticket_id": self.ticket_id,
            "event_type": self.event_type,
            "old_status": self.old_status or "",
            "new_status": self.new_status or "",
            "changed_by_role": self.changed_by_role,
            "actor_name": self.actor_name,
            "note": self.note or "",
            "created_at": self.created_at.isoformat() if self.created_at else "",
        }


class TicketAttachment(db.Model):
    """A file attached either to the initial ticket report or to a specific message."""
    __tablename__ = "ticket_attachments"

    id = db.Column(db.Integer, primary_key=True)
    ticket_id = db.Column(db.Integer, db.ForeignKey("tickets.id"), nullable=False)
    message_id = db.Column(db.Integer, db.ForeignKey("ticket_messages.id"), nullable=True)
    uploaded_by_role = db.Column(db.String(20), nullable=False)
    uploaded_by_employee_id = db.Column(db.Integer, db.ForeignKey("employees.id"), nullable=True)
    uploaded_by_admin_id = db.Column(db.Integer, db.ForeignKey("admins.id"), nullable=True)
    original_filename = db.Column(db.String(255), nullable=False)
    stored_filename = db.Column(db.String(255), nullable=False)
    content_type = db.Column(db.String(100), nullable=False)
    file_size_bytes = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, default=_utcnow)

    ticket = db.relationship("Ticket", back_populates="attachments")
    message = db.relationship("TicketMessage", back_populates="attachments")
    uploaded_by_employee = db.relationship("Employee")
    uploaded_by_admin = db.relationship("Admin")

    @property
    def uploader_name(self):
        if self.uploaded_by_role == "employee":
            return self.uploaded_by_employee.name if self.uploaded_by_employee else "Employee"
        return self.uploaded_by_admin.name if self.uploaded_by_admin else "IT Support"

    def to_dict(self):
        return {
            "id": self.id,
            "ticket_id": self.ticket_id,
            "message_id": self.message_id,
            "original_filename": self.original_filename,
            "content_type": self.content_type,
            "file_size_bytes": self.file_size_bytes,
            "uploader_name": self.uploader_name,
            "created_at": self.created_at.isoformat() if self.created_at else "",
        }


class Notification(db.Model):
    """Durable in-app notification for an employee or admin."""
    __tablename__ = "notifications"

    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("employees.id"), nullable=True, index=True)
    admin_id = db.Column(db.Integer, db.ForeignKey("admins.id"), nullable=True, index=True)
    title = db.Column(db.String(160), nullable=False)
    message = db.Column(db.Text, nullable=False)
    link_url = db.Column(db.String(255), nullable=True)
    is_read = db.Column(db.Boolean, nullable=False, default=False, index=True)
    created_at = db.Column(db.DateTime, default=_utcnow, index=True)

    employee = db.relationship("Employee")
    admin = db.relationship("Admin")

    @property
    def recipient_name(self):
        if self.employee_id:
            return self.employee.name if self.employee else "Employee"
        return self.admin.name if self.admin else "Administrator"

    def to_dict(self):
        return {
            "id": self.id, "title": self.title, "message": self.message,
            "link_url": self.link_url or "", "is_read": self.is_read,
            "created_at": self.created_at.isoformat() if self.created_at else "",
        }
