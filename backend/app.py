"""IT & Digital Asset Management System — Flask application factory and routes.

Stack: Flask + Jinja2 templates + MySQL (Flask-SQLAlchemy) + Tailwind CDN.
All routes are namespaced under /api so the platform ingress routes them to
this backend service.
"""
import mimetypes
import os
import secrets
import hmac
import uuid
import csv
import io
from datetime import date, datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from functools import wraps
from pathlib import Path

import bcrypt
from dotenv import load_dotenv
from flask import (Flask, abort, flash, jsonify, redirect, render_template,
                   request, send_from_directory, send_file, make_response, session, url_for)
from sqlalchemy import func, inspect, text
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.utils import secure_filename
from openpyxl import Workbook
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph
import qrcode

load_dotenv(Path(__file__).parent / ".env")

from models import (ASSET_CATEGORIES, ASSET_CONDITIONS, ASSET_STATUSES,  # noqa: E402
                    EMPLOYEE_ACCOUNT_STATUSES, EMPLOYEE_ALLOWED_TRANSITIONS,
                    TICKET_ATTACHMENT_EXTENSIONS, TICKET_ATTACHMENT_MAX_BYTES,
                    TICKET_CATEGORIES, TICKET_PRIORITIES, TICKET_STATUSES, Admin,
                    Asset, Assignment, Employee, Ticket, TicketAttachment,
                    TicketMessage, TicketStatusHistory, Notification, db)

BASE_DIR = Path(__file__).parent
# Phase 3: ticket attachments live outside static/templates and are only ever
# served through the authenticated download route below — never a static URL.
UPLOAD_ROOT = BASE_DIR / "uploads" / "tickets"
PROFILE_UPLOAD_ROOT = BASE_DIR / "uploads" / "profiles"
UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
PROFILE_UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)

flask_app = Flask(
    __name__,
    template_folder=str(BASE_DIR / "templates"),
    static_folder=str(BASE_DIR / "static"),
    static_url_path="/api/static",
)
flask_app.config["SECRET_KEY"] = os.environ["SECRET_KEY"]
flask_app.config["SQLALCHEMY_DATABASE_URI"] = os.environ["MYSQL_URL"]
flask_app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
flask_app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True, "pool_recycle": 280}
flask_app.config["APPLICATION_ROOT"] = "/api"
flask_app.config["SESSION_COOKIE_HTTPONLY"] = True
flask_app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
flask_app.config["SESSION_COOKIE_SECURE"] = os.environ.get("SESSION_COOKIE_SECURE", "0") == "1"
flask_app.config["TEMPLATES_AUTO_RELOAD"] = True
flask_app.jinja_env.auto_reload = True
# Phase 3: hard ceiling on any request body (ticket attachments). Slightly
# above TICKET_ATTACHMENT_MAX_BYTES to leave room for form fields; the real
# per-file 5MB limit is enforced explicitly in validate_attachment().
flask_app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024

db.init_app(flask_app)


# Lightweight dependency-free CSRF protection for all browser POST forms.
# A per-session token is rendered into pages and automatically submitted by
# the shared form helper in main.js / login pages. Non-browser requests may
# provide the same token through the X-CSRFToken header.
@flask_app.before_request
def ensure_csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    if request.method == "POST":
        supplied = request.form.get("csrf_token") or request.headers.get("X-CSRFToken")
        expected = session.get("csrf_token")
        if not supplied or not expected or not hmac.compare_digest(supplied, expected):
            abort(400, description="Invalid or missing CSRF token.")


DB_READY = {"ok": False, "error": ""}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        return False


def login_required(view):
    """Existing admin-auth decorator — behaviour unchanged from Phase 1.
    Kept exactly as-is so no currently-decorated admin route needs touching."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("admin_id"):
            if request.accept_mimetypes.best == "application/json":
                return jsonify({"error": "Not authenticated"}), 401
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapper


# Phase 2: explicit alias for new admin-only routes going forward. Identical
# check to login_required today; kept as a separate name so admin authorization
# can evolve independently of the legacy decorator without touching Phase 1 routes.
def admin_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("admin_id") or session.get("role") != "admin":
            if request.accept_mimetypes.best == "application/json":
                return jsonify({"error": "Not authenticated"}), 401
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapper


# Phase 2: employee-only routes. Checks the employee-specific session keys —
# an admin session (which never sets employee_id) cannot satisfy this, and an
# employee session cannot satisfy admin_required/login_required above, since
# admin_id and employee_id are never both set for the same login (see
# session.clear() calls in the login handlers below).
def employee_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("employee_id") or session.get("role") != "employee":
            if request.accept_mimetypes.best == "application/json":
                return jsonify({"error": "Not authenticated"}), 401
            return redirect(url_for("employee_login"))
        return view(*args, **kwargs)

    return wrapper


def parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Phase 3: Ticket helpers
# --------------------------------------------------------------------------- #
def generate_ticket_number():
    """TKT-<year>-00001, sequential within the current year, gap-free enough
    for this scale (single-writer MySQL session, low contention)."""
    year = datetime.now(timezone.utc).year
    prefix = f"TKT-{year}-"
    last = (
        Ticket.query.filter(Ticket.ticket_number.like(f"{prefix}%"))
        .order_by(Ticket.ticket_number.desc())
        .first()
    )
    next_seq = int(last.ticket_number.rsplit("-", 1)[1]) + 1 if last else 1
    return f"{prefix}{next_seq:05d}"


def validate_attachment(file_storage):
    """Extension + magic-byte content sniff + size check. Returns an error
    string, or None if the file is acceptable. No new dependency: uses stdlib
    mimetypes plus a first-bytes signature check rather than python-magic."""
    if not file_storage or not file_storage.filename:
        return None  # attachments are optional
    filename = secure_filename(file_storage.filename)
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in TICKET_ATTACHMENT_EXTENSIONS:
        return "Only JPG, JPEG, PNG, and PDF attachments are allowed."

    file_storage.stream.seek(0, os.SEEK_END)
    size = file_storage.stream.tell()
    file_storage.stream.seek(0)
    if size > TICKET_ATTACHMENT_MAX_BYTES:
        return "Attachment exceeds the 5 MB size limit."
    if size == 0:
        return "Attachment file is empty."

    header = file_storage.stream.read(12)
    file_storage.stream.seek(0)
    signatures = {
        "jpg": b"\xff\xd8\xff", "jpeg": b"\xff\xd8\xff",
        "png": b"\x89PNG\r\n\x1a\n",
        "pdf": b"%PDF-",
    }
    expected = signatures.get(ext)
    if expected and not header.startswith(expected):
        return "File content does not match its extension."
    return None


def save_ticket_attachment(ticket_id, file_storage, uploaded_by_role, employee_id=None, admin_id=None, message_id=None):
    """Writes the file to disk and returns an unsaved TicketAttachment ORM
    object. Caller is responsible for db.session.add()/commit() as part of
    the same transaction as the parent Ticket/TicketMessage row, and for
    calling delete_attachment_file() to clean up if the surrounding
    transaction is later rolled back."""
    filename = secure_filename(file_storage.filename)
    ext = filename.rsplit(".", 1)[-1].lower()
    stored_filename = f"{uuid.uuid4().hex}.{ext}"
    ticket_dir = UPLOAD_ROOT / str(ticket_id)
    ticket_dir.mkdir(parents=True, exist_ok=True)
    dest = ticket_dir / stored_filename
    file_storage.save(dest)

    content_type = file_storage.mimetype or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return TicketAttachment(
        ticket_id=ticket_id,
        message_id=message_id,
        uploaded_by_role=uploaded_by_role,
        uploaded_by_employee_id=employee_id,
        uploaded_by_admin_id=admin_id,
        original_filename=filename,
        stored_filename=stored_filename,
        content_type=content_type,
        file_size_bytes=dest.stat().st_size,
    )


def delete_attachment_file(ticket_id, stored_filename):
    """Best-effort cleanup of a file written to disk when the surrounding
    DB transaction fails and must be rolled back — files are not part of
    the SQL transaction, so this has to happen explicitly."""
    try:
        path = UPLOAD_ROOT / str(ticket_id) / stored_filename
        if path.exists():
            path.unlink()
    except OSError as exc:
        flask_app.logger.warning("Could not remove orphaned attachment %s: %s", stored_filename, exc)


# Every status change to a ticket MUST go through this function — it is the
# single place that (a) validates the transition is legal for the given
# actor role, (b) mutates ticket.status, and (c) writes the corresponding
# ticket_status_history row. No route sets ticket.status directly.
def transition_ticket_status(ticket, new_status, actor_role, admin_id=None, employee_id=None, note=None, event_type="STATUS_CHANGED"):
    old_status = ticket.status
    if actor_role == "employee":
        if (old_status, new_status) not in EMPLOYEE_ALLOWED_TRANSITIONS:
            raise ValueError("Employees may only reopen a resolved ticket.")
    elif new_status not in TICKET_STATUSES:
        raise ValueError("Invalid ticket status.")

    ticket.status = new_status
    if new_status == "Resolved":
        ticket.resolved_at = datetime.now(timezone.utc)
    if new_status == "Closed":
        ticket.closed_at = datetime.now(timezone.utc)
    if new_status == "Reopened":
        ticket.reopen_count += 1
        ticket.resolved_at = None
        ticket.closed_at = None

    db.session.add(TicketStatusHistory(
        ticket_id=ticket.id,
        event_type=event_type,
        old_status=old_status,
        new_status=new_status,
        changed_by_role=actor_role,
        changed_by_admin_id=admin_id,
        changed_by_employee_id=employee_id,
        note=note,
    ))


def format_ist_datetime(value):
    """Format stored UTC datetimes in India Standard Time for UI display.

    MySQL may return naive datetimes even though application timestamps are
    created in UTC, so a naive value is explicitly treated as UTC here.
    """
    if not value:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(ZoneInfo("Asia/Kolkata")).strftime("%d %b %Y, %I:%M %p")


flask_app.jinja_env.filters["ist_datetime"] = format_ist_datetime


def create_notification(*, employee_id=None, admin_id=None, title, message, link_url=None):
    if not employee_id and not admin_id:
        return
    db.session.add(Notification(
        employee_id=employee_id, admin_id=admin_id, title=title,
        message=message, link_url=link_url, is_read=False,
    ))


def notify_ticket_admins(title, message, link_url):
    for admin in Admin.query.all():
        create_notification(admin_id=admin.id, title=title, message=message, link_url=link_url)


@flask_app.context_processor
def inject_globals():
    employee_id = session.get("employee_id")
    admin_id = session.get("admin_id")
    employee_unread = admin_unread = 0
    # The global template context is also used by the DB error page, so a
    # failed notification query must never turn a graceful DB error into a
    # second exception while rendering the error template.
    if employee_id or admin_id:
        try:
            if employee_id:
                employee_unread = Notification.query.filter_by(employee_id=employee_id, is_read=False).count()
            if admin_id:
                admin_unread = Notification.query.filter_by(admin_id=admin_id, is_read=False).count()
        except SQLAlchemyError:
            employee_unread = admin_unread = 0
    return {
        "categories": ASSET_CATEGORIES,
        "statuses": ASSET_STATUSES,
        "conditions": ASSET_CONDITIONS,
        "account_statuses": EMPLOYEE_ACCOUNT_STATUSES,
        "ticket_categories": TICKET_CATEGORIES,
        "ticket_priorities": TICKET_PRIORITIES,
        "ticket_statuses": TICKET_STATUSES,
        "current_admin": session.get("admin_name"),
        "current_employee": session.get("employee_name"),
        "csrf_token": session.get("csrf_token"),
        "employee_unread_notifications": employee_unread,
        "admin_unread_notifications": admin_unread,
        "db_error": DB_READY["error"],
        "warranty_state": warranty_state,
    }


@flask_app.errorhandler(SQLAlchemyError)
def handle_db_error(exc):
    """Fail gracefully instead of leaking a stack trace when MySQL is down."""
    db.session.rollback()
    flask_app.logger.error("Database error: %s", exc)
    return render_template("error.html", message="Database connection error. Please verify MySQL is running."), 500


# --------------------------------------------------------------------------- #
# Bootstrap: create schema + seed admin/demo data
# --------------------------------------------------------------------------- #

# Phase 1 additive migration. db.create_all() only creates *missing tables* —
# it silently does nothing for columns added to models on tables that already
# exist. On a fresh/empty database create_all() already produces the final
# shape, so this is a no-op there; on an existing installation it adds only
# the specific columns below, never drops or recreates anything, and never
# touches existing data. Mirrors database/migrations/0001_phase1_assignment_enrichment.sql.
PHASE1_COLUMNS = {
    "assets": [
        ("condition_status", "VARCHAR(20) NULL"),
    ],
    "assignments": [
        ("expected_return_date", "DATE NULL"),
        ("condition_at_assignment", "VARCHAR(20) NULL"),
        ("remarks", "TEXT NULL"),
        ("assigned_by_admin_id", "INT NULL"),
    ],
}

# Phase 2: employee authentication columns.
# account_status ships with NOT NULL DEFAULT 'Inactive' directly in the DDL so
# that (a) existing employee rows are safely backfilled to 'Inactive' the
# moment the column is added, and (b) no employee can log in until an admin
# explicitly activates the account — matching the "no auto-activation" rule.
PHASE2_COLUMNS = {
    "employees": [
        ("password_hash", "VARCHAR(255) NULL"),
        ("account_status", "VARCHAR(20) NOT NULL DEFAULT 'Inactive'"),
    ],
}

PHASE4_COLUMNS = {
    "employees": [
        ("profile_image_filename", "VARCHAR(255) NULL"),
        ("profile_image_content_type", "VARCHAR(100) NULL"),
    ],
}

PHASE5_COLUMNS = {
    "assets": [("warranty_expiry", "DATE NULL"), ("warranty_provider", "VARCHAR(160) NULL")],
}

ALL_MIGRATION_COLUMNS = [PHASE1_COLUMNS, PHASE2_COLUMNS, PHASE4_COLUMNS, PHASE5_COLUMNS]


def run_additive_migrations():
    inspector = inspect(db.engine)
    existing_tables = set(inspector.get_table_names())

    for phase_columns in ALL_MIGRATION_COLUMNS:
        for table, columns in phase_columns.items():
            if table not in existing_tables:
                continue  # db.create_all() will have just created it with the full model shape
            existing_columns = {c["name"] for c in inspector.get_columns(table)}
            for column_name, column_ddl in columns:
                if column_name in existing_columns:
                    continue
                flask_app.logger.info("Migration: adding %s.%s", table, column_name)
                db.session.execute(text(f"ALTER TABLE {table} ADD COLUMN {column_name} {column_ddl}"))
                existing_columns.add(column_name)  # keep the local set in sync within this loop

    # FK for assigned_by_admin_id — added separately since it must run after the column exists,
    # and only if it isn't already present (MySQL doesn't support "ADD CONSTRAINT IF NOT EXISTS").
    if "assignments" in existing_tables:
        fk_names = {fk["name"] for fk in inspector.get_foreign_keys("assignments") if fk["name"]}
        if "fk_assignments_admin" not in fk_names:
            existing_columns = {c["name"] for c in inspector.get_columns("assignments")}
            if "assigned_by_admin_id" in existing_columns:
                try:
                    db.session.execute(text(
                        "ALTER TABLE assignments ADD CONSTRAINT fk_assignments_admin "
                        "FOREIGN KEY (assigned_by_admin_id) REFERENCES admins(id) ON DELETE SET NULL"
                    ))
                except SQLAlchemyError as exc:
                    # Non-fatal: some MySQL/MariaDB versions or permission setups may
                    # reject the constraint after the fact. The column itself still works.
                    flask_app.logger.warning("Could not add FK fk_assignments_admin: %s", exc)

    db.session.commit()


def init_database():
    with flask_app.app_context():
        try:
            db.create_all()
            run_additive_migrations()
            seed_admin()
            seed_demo_data()
            DB_READY["ok"] = True
            DB_READY["error"] = ""
        except SQLAlchemyError as exc:
            db.session.rollback()
            DB_READY["ok"] = False
            DB_READY["error"] = str(exc.__class__.__name__)
            flask_app.logger.error("Could not initialise database: %s", exc)


def seed_admin():
    email = os.environ["ADMIN_EMAIL"].lower()
    password = os.environ["ADMIN_PASSWORD"]
    admin = Admin.query.filter_by(email=email).first()
    if admin is None:
        db.session.add(Admin(email=email, name="Administrator", password_hash=hash_password(password)))
    elif not verify_password(password, admin.password_hash):
        admin.password_hash = hash_password(password)
    db.session.commit()


def seed_demo_data():
    if Employee.query.count() > 0 or Asset.query.count() > 0:
        return
    employees = [
        ("Aarav Mehta", "aarav.mehta@itam.com", "Engineering", "Backend Engineer"),
        ("Sofia Ramirez", "sofia.ramirez@itam.com", "Design", "Product Designer"),
        ("Liam O'Connor", "liam.oconnor@itam.com", "IT Operations", "Systems Admin"),
        ("Neha Kapoor", "neha.kapoor@itam.com", "Finance", "Financial Analyst"),
        ("Kenji Tanaka", "kenji.tanaka@itam.com", "Engineering", "QA Lead"),
        ("Amara Okafor", "amara.okafor@itam.com", "Human Resources", "HR Manager"),
    ]
    emp_objs = [Employee(name=n, email=e, department=d, job_title=t) for n, e, d, t in employees]
    db.session.add_all(emp_objs)
    db.session.flush()

    assets = [
        ("SN-LAP-1001", "MacBook Pro 16 M3", "Laptop", "Assigned", "2024-02-11", 0),
        ("SN-LAP-1002", "Dell XPS 15", "Laptop", "Assigned", "2023-11-04", 1),
        ("SN-LAP-1003", "Lenovo ThinkPad X1", "Laptop", "Available", "2024-06-19", None),
        ("SN-LAP-1004", "HP EliteBook 840", "Laptop", "Under Repair", "2022-09-30", None),
        ("SN-MON-2001", "Dell UltraSharp U2723QE", "Monitor", "Assigned", "2024-01-22", 2),
        ("SN-MON-2002", "LG 27UN880 UltraFine", "Monitor", "Available", "2024-03-08", None),
        ("SN-MON-2003", "Samsung Odyssey G7", "Monitor", "Under Repair", "2023-05-14", None),
        ("SN-DSK-3001", "Mac Studio M2 Max", "Desktop", "Assigned", "2024-04-02", 4),
        ("SN-DSK-3002", "HP Z2 Tower G9", "Desktop", "Available", "2023-08-27", None),
        ("SN-PRN-4001", "Brother HL-L3270CDW", "Printer", "Available", "2022-12-01", None),
        ("SN-PRN-4002", "Canon imageCLASS MF445dw", "Printer", "Under Repair", "2021-07-16", None),
        ("SN-MOB-5001", "iPhone 15 Pro", "Mobile Phone", "Assigned", "2024-05-21", 3),
        ("SN-MOB-5002", "Samsung Galaxy S24", "Mobile Phone", "Available", "2024-05-21", None),
        ("SN-TAB-6001", "iPad Air 11 M2", "Tablet", "Assigned", "2024-07-09", 5),
        ("SN-TAB-6002", "Surface Pro 9", "Tablet", "Available", "2023-10-12", None),
        ("SN-SRV-7001", "Dell PowerEdge R650", "Server", "Assigned", "2023-01-18", 2),
        ("SN-SRV-7002", "HPE ProLiant DL380", "Server", "Retired", "2019-04-25", None),
        ("SN-NET-8001", "Cisco Catalyst 9200", "Network Device", "Available", "2023-03-30", None),
        ("SN-NET-8002", "Ubiquiti UniFi Dream Machine", "Network Device", "Available", "2024-02-06", None),
        ("SN-SFT-9001", "Adobe Creative Cloud (5 seats)", "Software License", "Assigned", "2025-01-02", 1),
        ("SN-SFT-9002", "JetBrains All Products Pack", "Software License", "Available", "2025-01-02", None),
        ("SN-ACC-9501", "Logitech MX Master 3S", "Accessory", "Available", "2024-09-15", None),
    ]
    for serial, name, category, status, purchased, emp_idx in assets:
        asset = Asset(
            serial_number=serial,
            device_name=name,
            category=category,
            status=status,
            purchase_date=parse_date(purchased),
        )
        if status == "Assigned" and emp_idx is not None:
            asset.employee_id = emp_objs[emp_idx].id
            asset.assigned_date = date(2025, 1, 15)
        db.session.add(asset)
    db.session.commit()


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
@flask_app.route("/api/")
@flask_app.route("/api/home")
def public_home():
    return render_template("home.html")


@flask_app.route("/api/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        admin = Admin.query.filter_by(email=email).first()
        if admin and verify_password(password, admin.password_hash):
            session.clear()  # ensure no leftover employee identity coexists with this admin session
            session["admin_id"] = admin.id
            session["admin_name"] = admin.name
            session["role"] = "admin"
            return redirect(url_for("dashboard"))
        flash("Invalid email or password.", "error")
    return render_template("login.html")


@flask_app.route("/api/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# --------------------------------------------------------------------------- #
# Phase 2: Employee auth
# --------------------------------------------------------------------------- #
@flask_app.route("/api/employee/login", methods=["GET", "POST"])
def employee_login():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        employee = Employee.query.filter_by(email=email).first()

        # Single generic failure path for: unknown email, no password configured,
        # wrong password, and inactive account. Never reveal which case applies.
        valid = (
            employee is not None
            and employee.has_login_configured
            and employee.account_status == "Active"
            and verify_password(password, employee.password_hash)
        )
        if valid:
            session.clear()  # ensure no leftover admin identity coexists with this employee session
            session["employee_id"] = employee.id
            session["employee_name"] = employee.name
            session["role"] = "employee"
            return redirect(url_for("employee_dashboard"))
        flash("Invalid email or password.", "error")
    return render_template("employee_login.html")


@flask_app.route("/api/employee/logout")
def employee_logout():
    session.clear()
    return redirect(url_for("employee_login"))


# --------------------------------------------------------------------------- #
# Phase 5: Warranty, QR and export helpers
# --------------------------------------------------------------------------- #
def warranty_state(asset, today=None):
    today = today or date.today()
    if not asset.warranty_expiry:
        return {"label": "Not set", "class": "bg-slate-100 text-slate-500 ring-slate-200", "days": None}
    days = (asset.warranty_expiry - today).days
    if days < 0:
        return {"label": "Expired", "class": "bg-rose-50 text-rose-700 ring-rose-200", "days": days}
    if days <= 30:
        return {"label": f"Expires in {days}d", "class": "bg-amber-50 text-amber-700 ring-amber-200", "days": days}
    return {"label": "Active", "class": "bg-emerald-50 text-emerald-700 ring-emerald-200", "days": days}


def warranty_alert_assets():
    today = date.today()
    limit = today + timedelta(days=30)
    return (Asset.query.filter(Asset.warranty_expiry.isnot(None), Asset.warranty_expiry <= limit)
            .order_by(Asset.warranty_expiry.asc(), Asset.id.asc()).all())


def asset_export_rows(assets):
    rows = []
    for a in assets:
        w = warranty_state(a)
        rows.append([a.id, a.serial_number, a.device_name, a.category, a.status,
                     a.employee.name if a.employee else "",
                     a.purchase_date.isoformat() if a.purchase_date else "",
                     a.warranty_provider or "", a.warranty_expiry.isoformat() if a.warranty_expiry else "",
                     w["label"], a.condition_status or "", a.notes or ""])
    return rows


def asset_qr_payload(asset_id):
    return url_for("asset_qr_landing", asset_id=asset_id, _external=True)


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #
@flask_app.route("/api/dashboard")
@login_required
def dashboard():
    total = Asset.query.count()
    assigned = Asset.query.filter_by(status="Assigned").count()
    repair = Asset.query.filter_by(status="Under Repair").count()
    available = Asset.query.filter_by(status="Available").count()
    employees = Employee.query.count()
    today = date.today()
    warranty_expired = Asset.query.filter(Asset.warranty_expiry.isnot(None), Asset.warranty_expiry < today).count()
    warranty_due_30 = Asset.query.filter(Asset.warranty_expiry.isnot(None), Asset.warranty_expiry >= today, Asset.warranty_expiry <= today + timedelta(days=30)).count()
    warranty_alerts = warranty_alert_assets()[:6]

    by_category = (
        db.session.query(Asset.category, func.count(Asset.id))
        .group_by(Asset.category)
        .order_by(func.count(Asset.id).desc())
        .all()
    )
    recent = (
        Assignment.query.order_by(Assignment.created_at.desc(), Assignment.id.desc()).limit(6).all()
    )
    return render_template(
        "dashboard.html",
        active_page="dashboard",
        metrics={
            "total": total,
            "assigned": assigned,
            "repair": repair,
            "available": available,
            "employees": employees,
            "warranty_expired": warranty_expired,
            "warranty_due_30": warranty_due_30,
        },
        chart_labels=[c for c, _ in by_category],
        chart_values=[n for _, n in by_category],
        recent=recent,
        warranty_alerts=warranty_alerts,
    )


@flask_app.route("/api/stats")
@login_required
def stats():
    """JSON metrics endpoint used by the dashboard charts."""
    by_status = dict(db.session.query(Asset.status, func.count(Asset.id)).group_by(Asset.status).all())
    by_category = dict(db.session.query(Asset.category, func.count(Asset.id)).group_by(Asset.category).all())
    return jsonify({"by_status": by_status, "by_category": by_category, "total": Asset.query.count()})


# --------------------------------------------------------------------------- #
# Assets CRUD
# --------------------------------------------------------------------------- #
@flask_app.route("/api/assets")
@login_required
def assets_list():
    assets = Asset.query.order_by(Asset.id.desc()).all()
    employees = Employee.query.order_by(Employee.name).all()
    return render_template(
        "assets.html",
        active_page="assets",
        assets=assets,
        employees=employees,
        today=date.today().isoformat(),
        warranty_alerts=warranty_alert_assets(),
    )


@flask_app.route("/api/assets/create", methods=["POST"])
@login_required
def asset_create():
    serial = (request.form.get("serial_number") or "").strip()
    if not serial or not (request.form.get("device_name") or "").strip():
        flash("Serial number and device name are required.", "error")
        return redirect(url_for("assets_list"))
    if Asset.query.filter_by(serial_number=serial).first():
        flash(f"An asset with serial number {serial} already exists.", "error")
        return redirect(url_for("assets_list"))

    asset = Asset(
        serial_number=serial,
        device_name=request.form["device_name"].strip(),
        category=request.form.get("category") or "Laptop",
        status=request.form.get("status") or "Available",
        purchase_date=parse_date(request.form.get("purchase_date")),
        warranty_expiry=parse_date(request.form.get("warranty_expiry")),
        warranty_provider=(request.form.get("warranty_provider") or "").strip() or None,
        notes=(request.form.get("notes") or "").strip() or None,
    )
    db.session.add(asset)
    db.session.commit()
    flash(f"Asset {asset.device_name} created successfully.", "success")
    return redirect(url_for("assets_list"))


@flask_app.route("/api/assets/<int:asset_id>/update", methods=["POST"])
@login_required
def asset_update(asset_id):
    asset = db.session.get(Asset, asset_id) or abort(404)
    serial = (request.form.get("serial_number") or "").strip()
    clash = Asset.query.filter(Asset.serial_number == serial, Asset.id != asset.id).first()
    if clash:
        flash(f"Serial number {serial} is already used by another asset.", "error")
        return redirect(url_for("assets_list"))

    asset.serial_number = serial or asset.serial_number
    asset.device_name = (request.form.get("device_name") or asset.device_name).strip()
    asset.category = request.form.get("category") or asset.category
    new_status = request.form.get("status") or asset.status
    asset.purchase_date = parse_date(request.form.get("purchase_date"))
    asset.warranty_expiry = parse_date(request.form.get("warranty_expiry"))
    asset.warranty_provider = (request.form.get("warranty_provider") or "").strip() or None
    asset.notes = (request.form.get("notes") or "").strip() or None

    # Moving an asset out of "Assigned" releases the holder.
    if new_status != "Assigned" and asset.employee_id:
        asset.employee_id = None
        asset.assigned_date = None
    asset.status = new_status

    db.session.commit()
    flash("Asset updated successfully.", "success")
    return redirect(url_for("assets_list"))


@flask_app.route("/api/assets/<int:asset_id>/delete", methods=["POST"])
@login_required
def asset_delete(asset_id):
    asset = db.session.get(Asset, asset_id) or abort(404)
    Assignment.query.filter_by(asset_id=asset.id).delete()
    db.session.delete(asset)
    db.session.commit()
    flash("Asset deleted.", "success")
    return redirect(url_for("assets_list"))


@flask_app.route("/api/assets/<int:asset_id>/assign", methods=["POST"])
@login_required
def asset_assign(asset_id):
    asset = db.session.get(Asset, asset_id) or abort(404)
    if asset.status != "Available":
        flash("Only assets with status 'Available' can be assigned.", "error")
        return redirect(url_for("assets_list"))

    employee = db.session.get(Employee, int(request.form.get("employee_id") or 0))
    if employee is None:
        flash("Please select a valid employee.", "error")
        return redirect(url_for("assets_list"))

    assigned_on = parse_date(request.form.get("assigned_date")) or date.today()
    expected_return = parse_date(request.form.get("expected_return_date"))
    if request.form.get("expected_return_date") and expected_return is None:
        flash("Expected return date is invalid.", "error")
        return redirect(url_for("assets_list"))
    if expected_return and expected_return < assigned_on:
        flash("Expected return date cannot be before the assignment date.", "error")
        return redirect(url_for("assets_list"))

    condition = (request.form.get("condition_at_assignment") or "").strip()
    if condition and condition not in ASSET_CONDITIONS:
        flash("Please select a valid condition.", "error")
        return redirect(url_for("assets_list"))

    remarks = (request.form.get("remarks") or "").strip()
    if len(remarks) > 2000:
        flash("Remarks are too long (2000 character limit).", "error")
        return redirect(url_for("assets_list"))

    asset.employee_id = employee.id
    asset.assigned_date = assigned_on
    asset.status = "Assigned"
    if condition:
        asset.condition_status = condition
    db.session.add(Assignment(
        asset_id=asset.id,
        employee_id=employee.id,
        action="assigned",
        action_date=assigned_on,
        expected_return_date=expected_return,
        condition_at_assignment=condition or None,
        remarks=remarks or None,
        assigned_by_admin_id=session.get("admin_id"),
    ))
    db.session.commit()
    flash(f"{asset.device_name} assigned to {employee.name}.", "success")
    return redirect(url_for("assets_list"))


@flask_app.route("/api/assets/<int:asset_id>/unassign", methods=["POST"])
@login_required
def asset_unassign(asset_id):
    asset = db.session.get(Asset, asset_id) or abort(404)
    if not asset.employee_id:
        flash("This asset is not currently assigned.", "error")
        return redirect(url_for("assets_list"))
    db.session.add(Assignment(
        asset_id=asset.id,
        employee_id=asset.employee_id,
        action="returned",
        action_date=date.today(),
        assigned_by_admin_id=session.get("admin_id"),
    ))
    asset.employee_id = None
    asset.assigned_date = None
    asset.status = "Available"
    db.session.commit()
    flash("Asset returned to inventory.", "success")
    return redirect(url_for("assets_list"))


@flask_app.route("/api/assets/<int:asset_id>")
@login_required
def asset_detail(asset_id):
    asset = db.session.get(Asset, asset_id) or abort(404)
    history = (
        Assignment.query.filter_by(asset_id=asset.id)
        .order_by(Assignment.created_at.desc(), Assignment.id.desc())
        .all()
    )
    return render_template("asset_detail.html", active_page="assets", asset=asset, history=history, warranty=warranty_state(asset))


# --------------------------------------------------------------------------- #
# Employees CRUD
# --------------------------------------------------------------------------- #
@flask_app.route("/api/employees")
@login_required
def employees_list():
    employees = Employee.query.order_by(Employee.name).all()
    return render_template("employees.html", active_page="employees", employees=employees)


@flask_app.route("/api/employees/create", methods=["POST"])
@login_required
def employee_create():
    email = (request.form.get("email") or "").strip().lower()
    name = (request.form.get("name") or "").strip()
    if not name or not email:
        flash("Name and email are required.", "error")
        return redirect(url_for("employees_list"))
    if Employee.query.filter_by(email=email).first():
        flash("An employee with that email already exists.", "error")
        return redirect(url_for("employees_list"))
    db.session.add(
        Employee(
            name=name,
            email=email,
            department=(request.form.get("department") or "General").strip(),
            job_title=(request.form.get("job_title") or "").strip() or None,
        )
    )
    db.session.commit()
    flash(f"Employee {name} added.", "success")
    return redirect(url_for("employees_list"))


@flask_app.route("/api/employees/<int:employee_id>/update", methods=["POST"])
@login_required
def employee_update(employee_id):
    employee = db.session.get(Employee, employee_id) or abort(404)
    email = (request.form.get("email") or "").strip().lower()
    if Employee.query.filter(Employee.email == email, Employee.id != employee.id).first():
        flash("That email is already used by another employee.", "error")
        return redirect(url_for("employees_list"))
    employee.name = (request.form.get("name") or employee.name).strip()
    employee.email = email or employee.email
    employee.department = (request.form.get("department") or employee.department).strip()
    employee.job_title = (request.form.get("job_title") or "").strip() or None
    db.session.commit()
    flash("Employee updated.", "success")
    return redirect(url_for("employees_list"))


@flask_app.route("/api/employees/<int:employee_id>")
@login_required
def employee_detail(employee_id):
    employee = db.session.get(Employee, employee_id) or abort(404)
    current_assets = [a for a in employee.assets if a.status == "Assigned"]
    history = (
        Assignment.query.filter_by(employee_id=employee.id)
        .order_by(Assignment.created_at.desc(), Assignment.id.desc())
        .all()
    )
    return render_template(
        "employee_detail.html",
        active_page="employees",
        employee=employee,
        current_assets=current_assets,
        history=history,
    )


@flask_app.route("/api/employees/<int:employee_id>/delete", methods=["POST"])
@login_required
def employee_delete(employee_id):
    employee = db.session.get(Employee, employee_id) or abort(404)
    if any(a.status == "Assigned" for a in employee.assets):
        flash("Cannot delete an employee who still holds assigned assets.", "error")
        return redirect(url_for("employees_list"))
    Assignment.query.filter_by(employee_id=employee.id).delete()
    for asset in employee.assets:
        asset.employee_id = None
        asset.assigned_date = None
    db.session.delete(employee)
    db.session.commit()
    flash("Employee deleted.", "success")
    return redirect(url_for("employees_list"))


# --------------------------------------------------------------------------- #
# Phase 2: Admin-side employee account management (login credentials)
# --------------------------------------------------------------------------- #
@flask_app.route("/api/employees/<int:employee_id>/set-password", methods=["POST"])
@admin_required
def employee_set_password(employee_id):
    employee = db.session.get(Employee, employee_id) or abort(404)
    new_password = request.form.get("new_password") or ""
    confirm_password = request.form.get("confirm_password") or ""

    if len(new_password) < 8:
        flash("Password must be at least 8 characters long.", "error")
        return redirect(url_for("employees_list"))
    if new_password != confirm_password:
        flash("Password and confirmation do not match.", "error")
        return redirect(url_for("employees_list"))

    employee.password_hash = hash_password(new_password)
    db.session.commit()
    # Never log or flash the password value itself.
    flash(f"Login credentials updated for {employee.name}. Activate the account to allow sign-in.", "success")
    return redirect(url_for("employees_list"))


@flask_app.route("/api/employees/<int:employee_id>/activate", methods=["POST"])
@admin_required
def employee_activate(employee_id):
    employee = db.session.get(Employee, employee_id) or abort(404)
    if not employee.has_login_configured:
        flash(f"Set a password for {employee.name} before activating their account.", "error")
        return redirect(url_for("employees_list"))
    employee.account_status = "Active"
    db.session.commit()
    flash(f"{employee.name}'s account is now Active.", "success")
    return redirect(url_for("employees_list"))


@flask_app.route("/api/employees/<int:employee_id>/deactivate", methods=["POST"])
@admin_required
def employee_deactivate(employee_id):
    employee = db.session.get(Employee, employee_id) or abort(404)
    employee.account_status = "Inactive"
    db.session.commit()
    flash(f"{employee.name}'s account is now Inactive.", "success")
    return redirect(url_for("employees_list"))


# --------------------------------------------------------------------------- #
# Phase 2: Employee portal
# --------------------------------------------------------------------------- #
@flask_app.route("/api/employee/dashboard")
@employee_required
def employee_dashboard():
    employee = db.session.get(Employee, session["employee_id"]) or abort(404)
    my_assets = [a for a in employee.assets if a.employee_id == employee.id]
    my_tickets = Ticket.query.filter_by(employee_id=employee.id).all()
    return render_template(
        "employee/dashboard.html",
        employee=employee,
        metrics={
            "assigned": len([a for a in my_assets if a.status == "Assigned"]),
            "working": len([a for a in my_assets if a.condition_status in ("Working", "Good")]),
            "under_repair": len([a for a in my_assets if a.status == "Under Repair"]),
            "open_issues": len([t for t in my_tickets if t.status not in ("Resolved", "Closed")]),
            "resolved_issues": len([t for t in my_tickets if t.status in ("Resolved", "Closed")]),
        },
        my_assets=my_assets,
    )


@flask_app.route("/api/employee/profile")
@employee_required
def employee_profile():
    employee = db.session.get(Employee, session["employee_id"]) or abort(404)
    return render_template("employee/profile.html", employee=employee)


@flask_app.route("/api/employee/profile/photo", methods=["POST"])
@employee_required
def employee_profile_photo():
    employee = db.session.get(Employee, session["employee_id"]) or abort(404)
    file_storage = request.files.get("profile_image")
    if not file_storage or not file_storage.filename:
        flash("Please select an image.", "error")
        return redirect(url_for("employee_profile"))
    filename = secure_filename(file_storage.filename)
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in {"jpg", "jpeg", "png"}:
        flash("Only JPG, JPEG, and PNG profile images are allowed.", "error")
        return redirect(url_for("employee_profile"))
    file_storage.stream.seek(0, os.SEEK_END)
    size = file_storage.stream.tell()
    file_storage.stream.seek(0)
    if size > 2 * 1024 * 1024:
        flash("Profile image must be 2 MB or smaller.", "error")
        return redirect(url_for("employee_profile"))
    header = file_storage.stream.read(12)
    file_storage.stream.seek(0)
    signatures = {"jpg": b"\xff\xd8\xff", "jpeg": b"\xff\xd8\xff", "png": b"\x89PNG\r\n\x1a\n"}
    if not header.startswith(signatures[ext]):
        flash("The image content does not match its extension.", "error")
        return redirect(url_for("employee_profile"))
    stored = f"{uuid.uuid4().hex}.{ext}"
    dest = PROFILE_UPLOAD_ROOT / stored
    try:
        file_storage.save(dest)
        old = employee.profile_image_filename
        employee.profile_image_filename = stored
        employee.profile_image_content_type = file_storage.mimetype or mimetypes.guess_type(filename)[0] or "image/png"
        db.session.commit()
        if old:
            old_path = PROFILE_UPLOAD_ROOT / old
            if old_path.exists(): old_path.unlink()
        flash("Profile photo updated.", "success")
    except (OSError, SQLAlchemyError) as exc:
        db.session.rollback()
        if dest.exists(): dest.unlink()
        flask_app.logger.error("Profile photo upload failed: %s", exc)
        flash("Could not update the profile photo.", "error")
    return redirect(url_for("employee_profile"))


@flask_app.route("/api/employee/profile/photo/<int:employee_id>")
@employee_required
def employee_profile_photo_file(employee_id):
    if employee_id != session["employee_id"]:
        abort(404)
    employee = db.session.get(Employee, employee_id) or abort(404)
    if not employee.profile_image_filename:
        abort(404)
    return send_from_directory(PROFILE_UPLOAD_ROOT, employee.profile_image_filename, mimetype=employee.profile_image_content_type or "image/png")


@flask_app.route("/api/assets/<int:asset_id>/qr-view")
def asset_qr_landing(asset_id):
    asset = db.session.get(Asset, asset_id)
    if asset is None:
        abort(404)
    if session.get("admin_id") and session.get("role") == "admin":
        return redirect(url_for("asset_detail", asset_id=asset.id))
    if session.get("employee_id") and session.get("role") == "employee":
        if asset.employee_id != session["employee_id"]:
            abort(404)
        return redirect(url_for("employee_asset_detail", asset_id=asset.id))
    return redirect(url_for("public_home"))


@flask_app.route("/api/assets/<int:asset_id>/qr.png")
def asset_qr_image(asset_id):
    asset = db.session.get(Asset, asset_id)
    if asset is None:
        abort(404)
    allowed = (session.get("admin_id") and session.get("role") == "admin") or (session.get("employee_id") and session.get("role") == "employee" and asset.employee_id == session["employee_id"])
    if not allowed:
        abort(404)
    img = qrcode.make(asset_qr_payload(asset.id))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png", download_name=f"assetvault-asset-{asset.id}-qr.png")


@flask_app.route("/api/assets/export.csv")
@admin_required
def assets_export_csv():
    assets = Asset.query.order_by(Asset.id.asc()).all()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Asset ID","Serial Number","Device Name","Category","Status","Assigned Employee","Purchase Date","Warranty Provider","Warranty Expiry","Warranty Status","Condition","Notes"])
    writer.writerows(asset_export_rows(assets))
    response = make_response(buf.getvalue(), 200)
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = 'attachment; filename="assetvault_assets.csv"'
    return response


@flask_app.route("/api/assets/export.xlsx")
@admin_required
def assets_export_xlsx():
    assets = Asset.query.order_by(Asset.id.asc()).all()
    wb = Workbook()
    ws = wb.active
    ws.title = "Assets"
    headers = ["Asset ID","Serial Number","Device Name","Category","Status","Assigned Employee","Purchase Date","Warranty Provider","Warranty Expiry","Warranty Status","Condition","Notes"]
    ws.append(headers)
    for row in asset_export_rows(assets):
        ws.append(row)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for col in ws.columns:
        ws.column_dimensions[col[0].column_letter].width = min(max(len(str(c.value or "")) for c in col) + 2, 36)
    out = io.BytesIO(); wb.save(out); out.seek(0)
    return send_file(out, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", as_attachment=True, download_name="assetvault_assets.xlsx")


@flask_app.route("/api/assets/export.pdf")
@admin_required
def assets_export_pdf():
    assets = Asset.query.order_by(Asset.id.asc()).all()
    out = io.BytesIO()
    doc = SimpleDocTemplate(out, pagesize=landscape(A4), rightMargin=24, leftMargin=24, topMargin=24, bottomMargin=24)
    styles = getSampleStyleSheet()
    data = [["ID","Serial","Device","Category","Status","Employee","Warranty Expiry","Warranty Status"]]
    for a in assets:
        data.append([a.id, a.serial_number, a.device_name, a.category, a.status, a.employee.name if a.employee else "—", a.warranty_expiry.isoformat() if a.warranty_expiry else "—", warranty_state(a)["label"]])
    table = Table(data, repeatRows=1, colWidths=[30,78,120,70,68,95,78,80])
    table.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),colors.HexColor("#0f172a")),("TEXTCOLOR",(0,0),(-1,0),colors.white),("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),("FONTSIZE",(0,0),(-1,-1),7),("GRID",(0,0),(-1,-1),0.25,colors.HexColor("#cbd5e1")),("VALIGN",(0,0),(-1,-1),"MIDDLE"),("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white,colors.HexColor("#f8fafc")])]))
    doc.build([Paragraph("AssetVault — IT Asset Inventory Export", styles["Title"]), Paragraph(f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}", styles["Normal"]), table])
    out.seek(0)
    return send_file(out, mimetype="application/pdf", as_attachment=True, download_name="assetvault_assets.pdf")


@flask_app.route("/api/warranty-alerts")
@admin_required
def warranty_alerts_page():
    return render_template("warranty_alerts.html", active_page="warranty", assets=warranty_alert_assets())


@flask_app.route("/api/employee/assets")
@employee_required
def employee_assets():
    employee = db.session.get(Employee, session["employee_id"]) or abort(404)
    # Server-side scoping: only assets currently linked to this employee.
    my_assets = Asset.query.filter_by(employee_id=employee.id).order_by(Asset.id.desc()).all()
    return render_template("employee/assets.html", employee=employee, my_assets=my_assets)


@flask_app.route("/api/employee/assets/<int:asset_id>")
@employee_required
def employee_asset_detail(asset_id):
    asset = db.session.get(Asset, asset_id)
    # Ownership check enforced server-side. A non-existent asset and an asset
    # belonging to someone else both return 404 — the response never reveals
    # which case applies, so no information about other employees' assets leaks.
    if asset is None or asset.employee_id != session["employee_id"]:
        abort(404)
    latest_assignment = (
        Assignment.query.filter_by(asset_id=asset.id, employee_id=session["employee_id"], action="assigned")
        .order_by(Assignment.created_at.desc(), Assignment.id.desc())
        .first()
    )
    return render_template("employee/asset_detail.html", asset=asset, latest_assignment=latest_assignment, warranty=warranty_state(asset))


@flask_app.route("/api/employee/issues")
@employee_required
def employee_issues():
    tickets = (
        Ticket.query.filter_by(employee_id=session["employee_id"])
        .order_by(Ticket.created_at.desc())
        .all()
    )
    return render_template("employee/issues.html", tickets=tickets)


@flask_app.route("/api/employee/issues/new")
@employee_required
def employee_issue_new():
    employee = db.session.get(Employee, session["employee_id"]) or abort(404)
    my_assets = Asset.query.filter_by(employee_id=employee.id).order_by(Asset.device_name).all()
    preselect_asset_id = request.args.get("asset_id", type=int)
    return render_template("employee/issue_new.html", my_assets=my_assets, preselect_asset_id=preselect_asset_id)


@flask_app.route("/api/employee/issues/create", methods=["POST"])
@employee_required
def employee_issue_create():
    employee_id = session["employee_id"]
    employee = db.session.get(Employee, employee_id)
    if employee is None:
        session.clear()
        flash("Employee account could not be found.", "error")
        return redirect(url_for("employee_login"))

    asset_id = request.form.get("asset_id", type=int)
    asset = db.session.get(Asset, asset_id) if asset_id else None

    # Ownership enforced server-side: an employee can only file a ticket
    # against an asset currently assigned to them (requirement 1 / 10).
    if asset is None or asset.employee_id != employee_id:
        flash("You can only report issues for assets currently assigned to you.", "error")
        return redirect(url_for("employee_issues"))

    category = (request.form.get("category") or "").strip()
    priority = (request.form.get("priority") or "").strip()
    description = (request.form.get("description") or "").strip()
    remarks = (request.form.get("remarks") or "").strip()

    if category not in TICKET_CATEGORIES:
        flash("Please select a valid issue category.", "error")
        return redirect(url_for("employee_issue_new", asset_id=asset_id))
    if priority not in TICKET_PRIORITIES:
        flash("Please select a valid priority.", "error")
        return redirect(url_for("employee_issue_new", asset_id=asset_id))
    if not description:
        flash("Please describe the problem.", "error")
        return redirect(url_for("employee_issue_new", asset_id=asset_id))
    if len(description) > 4000 or len(remarks) > 2000:
        flash("Description or remarks are too long.", "error")
        return redirect(url_for("employee_issue_new", asset_id=asset_id))

    attachment_file = request.files.get("attachment")
    attachment_error = validate_attachment(attachment_file)
    if attachment_error:
        flash(attachment_error, "error")
        return redirect(url_for("employee_issue_new", asset_id=asset_id))

    full_description = description
    if remarks:
        full_description = f"{description}\n\nAdditional remarks: {remarks}"

    # --- Transactional creation (requirement 4): ticket + initial history row
    # are written together; the attachment file is only written to disk after
    # both DB rows are staged, and is cleaned up if the commit fails. ---
    saved_attachment_path = None
    try:
        ticket = Ticket(
            ticket_number=generate_ticket_number(),
            employee_id=employee_id,
            asset_id=asset.id,
            category=category,
            priority=priority,
            description=full_description,
            status="Open",
        )
        db.session.add(ticket)
        db.session.flush()  # assign ticket.id without committing yet

        db.session.add(TicketStatusHistory(
            ticket_id=ticket.id,
            event_type="STATUS_CHANGED",
            old_status=None,
            new_status="Open",
            changed_by_role="employee",
            changed_by_employee_id=employee_id,
            note="Ticket created.",
        ))

        if attachment_file and attachment_file.filename:
            attachment = save_ticket_attachment(
                ticket.id, attachment_file, "employee", employee_id=employee_id
            )
            saved_attachment_path = (ticket.id, attachment.stored_filename)
            db.session.add(attachment)

        db.session.commit()
    except (SQLAlchemyError, OSError) as exc:
        db.session.rollback()
        if saved_attachment_path:
            delete_attachment_file(*saved_attachment_path)
        flask_app.logger.error("Ticket creation failed: %s", exc)
        flash("Could not create the ticket due to a system error. Please try again.", "error")
        return redirect(url_for("employee_issue_new", asset_id=asset_id))

    notify_ticket_admins(
        title="New ticket reported",
        message=f"{employee.name} reported {ticket.ticket_number} for {asset.device_name}.",
        link_url=url_for("ticket_detail", ticket_id=ticket.id),
    )
    db.session.commit()
    flash(f"Ticket {ticket.ticket_number} created.", "success")
    return redirect(url_for("employee_issue_detail", ticket_id=ticket.id))


@flask_app.route("/api/employee/issues/<int:ticket_id>")
@employee_required
def employee_issue_detail(ticket_id):
    ticket = db.session.get(Ticket, ticket_id)
    if ticket is None or ticket.employee_id != session["employee_id"]:
        abort(404)
    return render_template("employee/issue_detail.html", ticket=ticket)


@flask_app.route("/api/employee/issues/<int:ticket_id>/message", methods=["POST"])
@employee_required
def employee_issue_message(ticket_id):
    ticket = db.session.get(Ticket, ticket_id)
    if ticket is None or ticket.employee_id != session["employee_id"]:
        abort(404)
    if ticket.status == "Closed":
        flash("This ticket is closed and can no longer receive messages.", "error")
        return redirect(url_for("employee_issue_detail", ticket_id=ticket.id))

    message_text = (request.form.get("message") or "").strip()
    if not message_text:
        flash("Message cannot be empty.", "error")
        return redirect(url_for("employee_issue_detail", ticket_id=ticket.id))
    if len(message_text) > 4000:
        flash("Message is too long.", "error")
        return redirect(url_for("employee_issue_detail", ticket_id=ticket.id))

    attachment_file = request.files.get("attachment")
    attachment_error = validate_attachment(attachment_file)
    if attachment_error:
        flash(attachment_error, "error")
        return redirect(url_for("employee_issue_detail", ticket_id=ticket.id))

    saved_attachment_path = None
    try:
        message = TicketMessage(
            ticket_id=ticket.id, sender_role="employee", employee_id=session["employee_id"], message=message_text,
        )
        db.session.add(message)
        db.session.flush()

        if attachment_file and attachment_file.filename:
            attachment = save_ticket_attachment(
                ticket.id, attachment_file, "employee", employee_id=session["employee_id"], message_id=message.id,
            )
            saved_attachment_path = (ticket.id, attachment.stored_filename)
            db.session.add(attachment)

        db.session.commit()
    except (SQLAlchemyError, OSError) as exc:
        db.session.rollback()
        if saved_attachment_path:
            delete_attachment_file(*saved_attachment_path)
        flask_app.logger.error("Ticket message failed: %s", exc)
        flash("Could not send the message due to a system error. Please try again.", "error")
        return redirect(url_for("employee_issue_detail", ticket_id=ticket.id))

    for admin in Admin.query.all():
        create_notification(admin_id=admin.id, title="New ticket message", message=f"{ticket.ticket_number} has a new message from {session.get('employee_name', 'an employee')}.", link_url=url_for("ticket_detail", ticket_id=ticket.id))
    db.session.commit()
    flash("Message sent.", "success")
    return redirect(url_for("employee_issue_detail", ticket_id=ticket.id))


@flask_app.route("/api/employee/issues/<int:ticket_id>/reopen", methods=["POST"])
@employee_required
def employee_issue_reopen(ticket_id):
    ticket = db.session.get(Ticket, ticket_id)
    if ticket is None or ticket.employee_id != session["employee_id"]:
        abort(404)

    reason = (request.form.get("reason") or "").strip()
    if not reason:
        flash("Please describe why you're reopening this ticket.", "error")
        return redirect(url_for("employee_issue_detail", ticket_id=ticket.id))
    if len(reason) > 2000:
        flash("Reopen reason is too long.", "error")
        return redirect(url_for("employee_issue_detail", ticket_id=ticket.id))

    try:
        transition_ticket_status(
            ticket, "Reopened", actor_role="employee", employee_id=session["employee_id"],
            note=reason, event_type="REOPENED",
        )
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "error")
        return redirect(url_for("employee_issue_detail", ticket_id=ticket.id))
    except SQLAlchemyError as exc:
        db.session.rollback()
        flask_app.logger.error("Ticket reopen failed: %s", exc)
        flash("Could not reopen the ticket due to a system error.", "error")
        return redirect(url_for("employee_issue_detail", ticket_id=ticket.id))

    notify_ticket_admins(title="Ticket reopened", message=f"{ticket.ticket_number} was reopened by {session.get('employee_name', 'an employee')}.", link_url=url_for("ticket_detail", ticket_id=ticket.id))
    db.session.commit()
    flash(f"Ticket {ticket.ticket_number} reopened.", "success")
    return redirect(url_for("employee_issue_detail", ticket_id=ticket.id))


@flask_app.route("/api/employee/notifications")
@employee_required
def employee_notifications():
    notifications = Notification.query.filter_by(employee_id=session["employee_id"]).order_by(Notification.created_at.desc()).all()
    return render_template("employee/notifications.html", notifications=notifications)


@flask_app.route("/api/employee/notifications/<int:notification_id>/read", methods=["POST"])
@employee_required
def employee_notification_read(notification_id):
    notification = Notification.query.filter_by(id=notification_id, employee_id=session["employee_id"]).first_or_404()
    notification.is_read = True
    db.session.commit()
    return redirect(request.form.get("next") or url_for("employee_notifications"))


@flask_app.route("/api/employee/notifications/read-all", methods=["POST"])
@employee_required
def employee_notifications_read_all():
    Notification.query.filter_by(employee_id=session["employee_id"], is_read=False).update({"is_read": True}, synchronize_session=False)
    db.session.commit()
    return redirect(url_for("employee_notifications"))


# --------------------------------------------------------------------------- #
# Phase 3: Admin Helpdesk / ticket management
# --------------------------------------------------------------------------- #
@flask_app.route("/api/notifications")
@admin_required
def admin_notifications():
    notifications = Notification.query.filter_by(admin_id=session["admin_id"]).order_by(Notification.created_at.desc()).all()
    return render_template("admin_notifications.html", notifications=notifications)


@flask_app.route("/api/notifications/<int:notification_id>/read", methods=["POST"])
@admin_required
def admin_notification_read(notification_id):
    notification = Notification.query.filter_by(id=notification_id, admin_id=session["admin_id"]).first_or_404()
    notification.is_read = True
    db.session.commit()
    return redirect(request.form.get("next") or url_for("admin_notifications"))


@flask_app.route("/api/notifications/read-all", methods=["POST"])
@admin_required
def admin_notifications_read_all():
    Notification.query.filter_by(admin_id=session["admin_id"], is_read=False).update({"is_read": True}, synchronize_session=False)
    db.session.commit()
    return redirect(url_for("admin_notifications"))


@flask_app.route("/api/tickets")
@admin_required
def tickets_list():
    query = Ticket.query

    ticket_number = (request.args.get("ticket_number") or "").strip()
    employee_id = request.args.get("employee_id", type=int)
    asset_id = request.args.get("asset_id", type=int)
    department = (request.args.get("department") or "").strip()
    category = request.args.get("category") or ""
    priority = request.args.get("priority") or ""
    status = request.args.get("status") or ""
    date_from = parse_date(request.args.get("date_from"))
    date_to = parse_date(request.args.get("date_to"))

    if ticket_number:
        query = query.filter(Ticket.ticket_number.ilike(f"%{ticket_number}%"))
    if employee_id:
        query = query.filter(Ticket.employee_id == employee_id)
    if asset_id:
        query = query.filter(Ticket.asset_id == asset_id)
    if department:
        query = query.join(Employee, Ticket.employee_id == Employee.id).filter(Employee.department == department)
    if category in TICKET_CATEGORIES:
        query = query.filter(Ticket.category == category)
    if priority in TICKET_PRIORITIES:
        query = query.filter(Ticket.priority == priority)
    if status in TICKET_STATUSES:
        query = query.filter(Ticket.status == status)
    if date_from:
        query = query.filter(func.date(Ticket.created_at) >= date_from)
    if date_to:
        query = query.filter(func.date(Ticket.created_at) <= date_to)

    tickets = query.order_by(Ticket.created_at.desc()).all()

    all_tickets = Ticket.query.all()
    metrics = {
        "total": len(all_tickets),
        "open": len([t for t in all_tickets if t.status == "Open"]),
        "critical": len([t for t in all_tickets if t.priority == "Critical" and t.status not in ("Closed",)]),
        "in_progress": len([t for t in all_tickets if t.status == "In Progress"]),
        "waiting_part": len([t for t in all_tickets if t.status == "Waiting For Part"]),
        "resolved": len([t for t in all_tickets if t.status == "Resolved"]),
        "closed": len([t for t in all_tickets if t.status == "Closed"]),
    }
    employees = Employee.query.order_by(Employee.name).all()
    departments = sorted({e.department for e in employees if e.department})

    return render_template(
        "tickets.html", active_page="tickets", tickets=tickets, metrics=metrics, employees=employees,
        departments=departments,
        filters={
            "ticket_number": ticket_number, "employee_id": employee_id, "asset_id": asset_id,
            "department": department, "category": category, "priority": priority, "status": status,
            "date_from": request.args.get("date_from") or "", "date_to": request.args.get("date_to") or "",
        },
    )


@flask_app.route("/api/tickets/<int:ticket_id>")
@admin_required
def ticket_detail(ticket_id):
    ticket = db.session.get(Ticket, ticket_id) or abort(404)
    admins = Admin.query.order_by(Admin.name).all()
    return render_template("ticket_detail.html", active_page="tickets", ticket=ticket, admins=admins)


@flask_app.route("/api/tickets/<int:ticket_id>/message", methods=["POST"])
@admin_required
def ticket_message(ticket_id):
    ticket = db.session.get(Ticket, ticket_id) or abort(404)
    message_text = (request.form.get("message") or "").strip()
    if not message_text:
        flash("Message cannot be empty.", "error")
        return redirect(url_for("ticket_detail", ticket_id=ticket.id))
    if len(message_text) > 4000:
        flash("Message is too long.", "error")
        return redirect(url_for("ticket_detail", ticket_id=ticket.id))

    attachment_file = request.files.get("attachment")
    attachment_error = validate_attachment(attachment_file)
    if attachment_error:
        flash(attachment_error, "error")
        return redirect(url_for("ticket_detail", ticket_id=ticket.id))

    saved_attachment_path = None
    try:
        message = TicketMessage(
            ticket_id=ticket.id, sender_role="admin", admin_id=session["admin_id"], message=message_text,
        )
        db.session.add(message)
        db.session.flush()

        if attachment_file and attachment_file.filename:
            attachment = save_ticket_attachment(
                ticket.id, attachment_file, "admin", admin_id=session["admin_id"], message_id=message.id,
            )
            saved_attachment_path = (ticket.id, attachment.stored_filename)
            db.session.add(attachment)

        db.session.commit()
    except (SQLAlchemyError, OSError) as exc:
        db.session.rollback()
        if saved_attachment_path:
            delete_attachment_file(*saved_attachment_path)
        flask_app.logger.error("Admin ticket message failed: %s", exc)
        flash("Could not send the message due to a system error.", "error")
        return redirect(url_for("ticket_detail", ticket_id=ticket.id))

    create_notification(employee_id=ticket.employee_id, title="Ticket reply received", message=f"An admin replied to {ticket.ticket_number}.", link_url=url_for("employee_issue_detail", ticket_id=ticket.id))
    db.session.commit()
    flash("Reply sent.", "success")
    return redirect(url_for("ticket_detail", ticket_id=ticket.id))


@flask_app.route("/api/tickets/<int:ticket_id>/status", methods=["POST"])
@admin_required
def ticket_status_change(ticket_id):
    ticket = db.session.get(Ticket, ticket_id) or abort(404)
    new_status = request.form.get("status") or ""
    note = (request.form.get("note") or "").strip() or None

    event_type = {"Resolved": "RESOLVED", "Closed": "CLOSED", "Reopened": "REOPENED"}.get(new_status, "STATUS_CHANGED")
    if new_status == "Resolved":
        resolution_notes = (request.form.get("resolution_notes") or "").strip()
        if not resolution_notes:
            flash("Please add resolution notes before marking this ticket resolved.", "error")
            return redirect(url_for("ticket_detail", ticket_id=ticket.id))
        ticket.resolution_notes = resolution_notes
        note = note or "Marked resolved."

    try:
        transition_ticket_status(
            ticket, new_status, actor_role="admin", admin_id=session["admin_id"], note=note, event_type=event_type,
        )
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "error")
        return redirect(url_for("ticket_detail", ticket_id=ticket.id))
    except SQLAlchemyError as exc:
        db.session.rollback()
        flask_app.logger.error("Ticket status change failed: %s", exc)
        flash("Could not update ticket status due to a system error.", "error")
        return redirect(url_for("ticket_detail", ticket_id=ticket.id))

    event_titles = {
        "Resolved": "Ticket resolved",
        "Closed": "Ticket closed",
        "Reopened": "Ticket reopened",
    }
    event_messages = {
        "Resolved": f"{ticket.ticket_number} has been resolved. Review the resolution details.",
        "Closed": f"{ticket.ticket_number} has been closed by the support team.",
        "Reopened": f"{ticket.ticket_number} has been reopened and returned to the support workflow.",
    }
    create_notification(
        employee_id=ticket.employee_id,
        title=event_titles.get(new_status, "Ticket status updated"),
        message=event_messages.get(new_status, f"{ticket.ticket_number} is now {new_status}."),
        link_url=url_for("employee_issue_detail", ticket_id=ticket.id),
    )
    db.session.commit()
    flash(f"Ticket {ticket.ticket_number} status changed to {new_status}.", "success")
    return redirect(url_for("ticket_detail", ticket_id=ticket.id))


@flask_app.route("/api/tickets/<int:ticket_id>/assign", methods=["POST"])
@admin_required
def ticket_assign(ticket_id):
    ticket = db.session.get(Ticket, ticket_id) or abort(404)
    admin_id = request.form.get("admin_id", type=int)
    admin = db.session.get(Admin, admin_id) if admin_id else None
    if admin is None:
        flash("Please select a valid support admin.", "error")
        return redirect(url_for("ticket_detail", ticket_id=ticket.id))

    ticket.assigned_admin_id = admin.id
    db.session.add(TicketStatusHistory(
        ticket_id=ticket.id,
        event_type="ASSIGNED",
        changed_by_role="admin",
        changed_by_admin_id=session["admin_id"],
        note=f"Assigned to {admin.name}.",
    ))
    db.session.commit()
    create_notification(employee_id=ticket.employee_id, title="Ticket assigned", message=f"{ticket.ticket_number} has been assigned to {admin.name}.", link_url=url_for("employee_issue_detail", ticket_id=ticket.id))
    db.session.commit()
    flash(f"Ticket {ticket.ticket_number} assigned to {admin.name}.", "success")
    return redirect(url_for("ticket_detail", ticket_id=ticket.id))


@flask_app.route("/api/tickets/<int:ticket_id>/expected-resolution", methods=["POST"])
@admin_required
def ticket_expected_resolution(ticket_id):
    ticket = db.session.get(Ticket, ticket_id) or abort(404)
    new_date = parse_date(request.form.get("expected_resolution_date"))
    if request.form.get("expected_resolution_date") and new_date is None:
        flash("Expected resolution date is invalid.", "error")
        return redirect(url_for("ticket_detail", ticket_id=ticket.id))

    old_date = ticket.expected_resolution_date
    ticket.expected_resolution_date = new_date
    note = (
        f"Expected resolution date changed from {old_date.isoformat() if old_date else 'unset'} "
        f"to {new_date.isoformat() if new_date else 'unset'}."
    )
    db.session.add(TicketStatusHistory(
        ticket_id=ticket.id,
        event_type="EXPECTED_DATE_CHANGED",
        changed_by_role="admin",
        changed_by_admin_id=session["admin_id"],
        note=note,
    ))
    db.session.commit()
    create_notification(employee_id=ticket.employee_id, title="Expected resolution date changed", message=f"{ticket.ticket_number}: {note}", link_url=url_for("employee_issue_detail", ticket_id=ticket.id))
    db.session.commit()
    flash("Expected resolution date updated.", "success")
    return redirect(url_for("ticket_detail", ticket_id=ticket.id))


@flask_app.route("/api/attachments/<int:attachment_id>")
def attachment_download(attachment_id):
    # Manual auth check here (rather than login_required/employee_required)
    # because this single route must serve both admin and employee sessions,
    # each with a different ownership rule.
    attachment = db.session.get(TicketAttachment, attachment_id) or abort(404)
    ticket = attachment.ticket

    if session.get("role") == "admin" and session.get("admin_id"):
        pass  # admin can download attachments on any ticket
    elif session.get("role") == "employee" and session.get("employee_id") == ticket.employee_id:
        pass  # employee can download attachments only on their own ticket
    else:
        abort(404)  # never confirm the attachment/ticket exists to an unauthorized caller

    directory = UPLOAD_ROOT / str(ticket.id)
    return send_from_directory(
        directory, attachment.stored_filename, as_attachment=True, download_name=attachment.original_filename,
    )


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #
@flask_app.route("/api/health")
def health():
    try:
        db.session.execute(db.text("SELECT 1"))
        return jsonify({"status": "ok", "database": "mysql", "time": datetime.now(timezone.utc).isoformat()})
    except SQLAlchemyError as exc:
        return jsonify({"status": "degraded", "error": exc.__class__.__name__}), 503


init_database()

if __name__ == "__main__":
    flask_app.run(host="0.0.0.0", port=8001, debug=True)
