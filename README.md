# IT Asset Management System

AssetVault IT & Digital Asset Management System. This package preserves the Flask/Jinja2 + MySQL/MariaDB application and the React redirect shell from the supplied project.

## Stack
- Backend: Python, Flask, Flask-SQLAlchemy, Jinja2
- Database: MySQL/MariaDB
- Frontend shell: React
- UI: Tailwind CDN, DataTables, Chart.js

## Admin login
Admin credentials are configured through `backend/.env` using `ADMIN_EMAIL` and `ADMIN_PASSWORD`. Do not commit or share the real `.env` file.

## Local backend run (recommended)
1. Install Python 3.11+ and MySQL/MariaDB.
2. Create database/user using `scripts/init_mysql.sh` on Linux/macOS, or execute its SQL manually in MySQL on Windows.
3. Open `backend/.env` and keep the supplied local MySQL settings unless you use different credentials.
4. In VS Code terminal:
   - `cd backend`
   - `python -m venv .venv`
   - Windows: `.venv\Scripts\activate` ; macOS/Linux: `source .venv/bin/activate`
   - `pip install -r requirements.txt`
   - `python app.py`
5. Open `http://127.0.0.1:8001/api/login`.

The application automatically creates its tables and seeds the administrator/demo data on first successful database connection.

## Optional React shell
The core application is server-rendered Flask. The React shell only redirects to the backend `/api/` URL. If you want to run it separately, set `REACT_APP_BACKEND_URL=http://127.0.0.1:8001` in `frontend/.env.local`, then run `npm install` and `npm start` inside `frontend`.

## Important
No application features, routes, data model, UI pages, login credentials, asset categories/statuses, or assignment behaviour from the supplied source were intentionally changed. The extra files only make the supplied project reproducible locally in VS Code.

## Phase 1 — Asset ↔ Employee Assignment Enrichment

Phase 1 extends (does not replace) the existing assignment system:

- **Assign modal** (`/api/assets/<id>/assign`) now also accepts Expected Return Date, Condition at Assignment (`Working`/`Good`/`Fair`/`Damaged`/`Not Working`), and Remarks.
- **`assets.condition_status`** — new nullable column holding the asset's current condition.
- **`assignments`** table — new nullable columns: `expected_return_date`, `condition_at_assignment`, `remarks`, `assigned_by_admin_id` (the admin who performed the action).
- **New pages**: `/api/assets/<id>` (Asset Detail — full field set, current assignment, assignment history) and `/api/employees/<id>` (Employee Detail — current assets, assignment history). Both are linked from the existing asset/employee list rows.
- **Existing routes, columns, and behaviour are unchanged** — asset/employee CRUD, assign/return flow, dashboard, and login all work exactly as before.

### Applying the migration on an existing database
The app auto-runs an additive migration on startup (`run_additive_migrations()` in `app.py`) that adds only the new nullable columns above — it never drops or recreates a table and never touches existing rows.

If you'd rather apply it manually (e.g. in production, ahead of a deploy), run:
```
mysql -u itam_user -p itam_db < database/migrations/0001_phase1_assignment_enrichment.sql
```
This SQL file is idempotent — safe to run more than once. **Back up your database before running any migration.**

### What's still not in this codebase
QR asset tracking, warranty tracking, and PDF/Excel export were intentionally deferred until Phase 5. They are now implemented as real features.

## Phase 2 — Employee Authentication & Role-Based Access

- **Two independent login flows**: `/api/login` (admin, unchanged) and `/api/employee/login` (new). Each sets its own session identity — `session["admin_id"]`/`session["role"]="admin"` vs `session["employee_id"]`/`session["role"]="employee"`. Neither session type can satisfy the other's authorization checks; every login clears the session first so the two identities can never coexist.
- **`employees.password_hash`** (nullable) and **`employees.account_status`** (`Active`/`Inactive`, defaults to `Inactive`) — new additive columns. No existing employee is auto-activated or given a default password; they show **"Login Not Configured"** in the admin Employees screen until an admin explicitly sets a password there.
- **Admin account management**: on the Employees screen, admin can set/reset a password (bcrypt-hashed, never displayed or logged) and activate/deactivate accounts, all via POST.
- **Login rejection is generic** — wrong password, unconfigured login, and inactive account all show the same "Invalid email or password" message; the system never reveals which case applies.
- **Employee portal** (`/api/employee/dashboard`, `/api/employee/profile`, `/api/employee/assets`, `/api/employee/assets/<id>`): every asset route is scoped server-side to `asset.employee_id == session["employee_id"]`; requesting another employee's asset returns 404, not an error page that reveals the asset exists.
- **`My Issues`** and **`Notifications`** are implemented in Phase 3/4 with server-side ownership checks and durable database notifications.
- **New migration**: `database/migrations/0002_employee_auth.sql`, same idempotent guarded pattern as `0001`.

### Known limitation
If an admin was already logged in (browser session cookie) before this Phase 2 deploy, that old cookie has `admin_id` but no `role` key, since `role` is only set on a fresh login. It still works on every pre-existing admin route (`login_required` only checks `admin_id`), but the three new `admin_required` account-management routes (set password / activate / deactivate) will redirect that stale session back to login once. Logging in again resolves it — this does not affect any Phase 1 functionality.

## Phase 3 — Asset Issue Reporting + Helpdesk / Ticket Management

A complete helpdesk sits on top of the Phase 1 asset↔employee link and Phase 2 employee login. Four new tables, no changes to any Phase 1/2 table.

### Ticket system
- **`tickets`** — one row per reported issue: `ticket_number` (`TKT-<year>-00001`, sequential per year), `employee_id`/`asset_id` FKs (no duplicated employee/asset data), `category`, `priority`, `description`, `status`, `assigned_admin_id`, `expected_resolution_date`, `resolution_notes`, `resolved_at`/`closed_at`, `reopen_count`.
- **`ticket_messages`** — the conversation thread. Each row is attributed to exactly one sender (`employee_id` xor `admin_id`, driven by `sender_role`).
- **`ticket_status_history`** — an append-only audit trail. Every status change, assignment, expected-date edit, resolve, close, and reopen writes exactly one row here, tagged with an `event_type` (`STATUS_CHANGED` / `ASSIGNED` / `EXPECTED_DATE_CHANGED` / `RESOLVED` / `CLOSED` / `REOPENED`) so non-status events never masquerade as a status transition.
- **`ticket_attachments`** — files attached either to the initial report (`message_id = NULL`) or to a specific reply (`message_id` set), so the UI can show each attachment next to the exact message it belongs to.

All ticket status changes go through a single backend function (`transition_ticket_status()`), which is the only code path allowed to mutate `ticket.status` — this is what guarantees the history table can never fall out of sync with the actual status.

### Employee issue reporting
From **My Assets** or an asset's detail page, employees see a **Report an Issue** button — only ever pointed at assets currently assigned to them; the server re-checks ownership on submit regardless of what the form claims. The report form captures category, priority, description, optional remarks, and an optional attachment, and creates the ticket, its first history row, and any attachment file **transactionally** — if anything fails partway through, the database change is rolled back and any file already written to disk is deleted, so a failed submission never leaves an orphaned file or a ticket without its initial history row.

### Ticket lifecycle
```
Open → Acknowledged → In Progress → Waiting For Part → Resolved → Closed
                                                            ↓
                                                        Reopened
```
Employees may only trigger one transition — `Resolved → Reopened` — and only on their own ticket, and only with a required, length-limited reason (stored in the history row's `note`). Every other transition is admin-only. Resolving a ticket requires resolution notes.

### Admin Helpdesk
`/api/tickets` — dashboard with per-status/priority summary cards and filters (Ticket ID, Employee, Department, Asset, Category, Priority, Status, date range). From a ticket's detail page, admin can reply, change status, assign a support admin, set/change the expected resolution date, resolve (with notes), and close — each of these actions is logged to `ticket_status_history`.

### Attachments
JPG/JPEG/PNG/PDF only, 5 MB max, validated by extension **and** a magic-byte signature check (not just the filename), stored under `backend/uploads/tickets/<ticket_id>/` with a random UUID filename — **never** served as a static file. Every download goes through the authenticated `/api/attachments/<id>` route, which re-checks that the requester (admin, or the employee who owns the ticket) is allowed to see that specific attachment before streaming it.

### Migration 0003
`database/migrations/0003_ticket_system.sql` creates the four tables above with `CREATE TABLE IF NOT EXISTS` — natively idempotent, no existing table touched. On the app side, since these are brand-new tables (not new columns on existing tables), Flask-SQLAlchemy's `db.create_all()` already creates them for free on both fresh and existing databases at startup — the SQL file is for manual/production application and documentation.

### Local upload directory
`backend/uploads/` is created automatically at startup if missing, and is excluded from version control via `.gitignore` (`backend/uploads/`) — uploaded files never get committed.

### Testing this phase
1. As an employee: My Assets → Report an Issue on an assigned asset → fill the form → confirm the ticket appears in My Issues with the correct `TKT-2026-XXXXX` number.
2. Open the ticket → confirm the timeline shows a single "Ticket created" entry, the description matches, and any attachment is downloadable.
3. Send a follow-up message with an attachment → confirm it appears in the conversation immediately below where you'd expect.
4. As admin: Helpdesk → confirm the new ticket appears, summary cards update, and each filter (including Department) narrows the list correctly.
5. Open the ticket → reply, assign a support admin, set an expected resolution date, then change status through a few stages → confirm each action adds a distinct timeline entry with the correct `event_type`.
6. Resolve the ticket without resolution notes → confirm it's rejected; add notes → confirm it resolves.
7. As the employee, reopen the resolved ticket without a reason → confirm it's rejected; with a reason → confirm status flips to `Reopened` and the reason appears in the timeline.
8. Try to open another employee's ticket by guessing its URL (as an employee) → confirm 404.
9. Try to download another employee's ticket attachment by guessing its `/api/attachments/<id>` URL → confirm 404.
10. Re-run the full Phase 1 and Phase 2 test lists from their respective sections above — confirm nothing regressed.

## Phase 4 — Public Website, Notifications & Employee Profile Photos

Phase 4 upgrades AssetVault with a professional public landing page and a durable in-app notification system while preserving the Phase 1–3 admin, employee and helpdesk workflows.

### Public website
- `/api/` and `/api/home` are public guest-facing landing pages.
- Navigation sections: Home, Vision, Mission, Objectives, Features, FAQs and Contact.
- Admin Login and Employee Login are available from the public page.
- Lightweight splash/opening animation and responsive layout are included.
- Guest navigation remains a top navigation bar (best suited to a public landing page); a responsive mobile menu is included for smaller screens.
- The hero overview, objectives and feature cards now have useful guest-facing shortcuts instead of being dead/static boxes.
- FAQs now include two additional help topics plus a lightweight local “Ask AssetVault” FAQ assistant. It is intentionally a demo/static assistant, not a fake live-agent chat.
- Footer now includes Customer Support, clickable demo contact details, address/project context, quick links and both portal entry points.
- Public contact details are explicitly marked as academic-demo details; replace them with college-approved real details before final submission if available.

### Notifications
- New `notifications` table stores employee/admin notifications, read state, message and optional deep link.
- Ticket creation and employee replies notify administrators.
- Admin replies, status changes, assignments and expected-resolution-date changes notify the affected employee.
- Employees and admins each have a notification page with unread badges and Mark Read / Mark All Read actions.
- Notifications are durable database records rather than browser-only alerts.

### Employee profile photo
- Employees can upload JPG/JPEG/PNG profile images from My Profile.
- Maximum size: 2 MB.
- Extension and magic-byte validation are applied before saving.
- Files are stored under `backend/uploads/profiles/`, outside the static directory, and served only to the authenticated owner.
- Existing employees remain valid because the new columns are nullable.

### Phase 4 migration
`database/migrations/0004_phase4.sql` adds the two employee profile-image columns and creates the `notifications` table. On application startup, missing columns are added additively and `db.create_all()` creates the new notification table when it does not exist.

### Phase 4 verification checklist
1. Open `/api/` without logging in and confirm the public home page loads.
2. Confirm Admin Login and Employee Login links work.
3. Log in as an employee and upload a JPG/PNG profile photo under My Profile.
4. Try an invalid extension and an oversized image; both should be rejected.
5. Report a ticket as an employee and confirm the admin notification appears.
6. Reply/status-change/assign/set expected date from the admin portal and confirm employee notifications appear.
7. Verify unread notification badges and Mark Read / Mark All Read.
8. Confirm an employee cannot fetch another employee's profile photo URL (should return 404).
9. Re-run the Phase 1–3 CRUD, ticket ownership and attachment tests.
10. Test the mobile navigation, hero cards, objective/feature shortcuts, FAQ assistant, mailto/tel links and footer portal links.
11. Replace the academic-demo public contact details with final college-project-approved details before submission if you have approved real details.


## Phase 5 — QR Asset Tracking, Warranty Alerts & Exports

Phase 5 turns QR tracking, warranty monitoring and inventory exports into working features.

### QR Asset Tracking
- Every asset detail page shows a generated QR code.
- The QR encodes an authenticated asset-view URL instead of exposing private asset data directly.
- Admins can access any asset; employees can access only assets currently assigned to themselves.
- QR images are generated on demand and are not public static files.

### Warranty Alerts
- Assets support `warranty_provider` and `warranty_expiry`.
- Admins can set/edit warranty information from the asset form.
- Inventory badges show Active, Expires in N days, Expired, or Not set.
- Dashboard and Warranty Alerts show expired warranties and warranties expiring within 30 days.

### Exports
Admin users can export the complete inventory as Excel (`.xlsx`), PDF (`.pdf`) and CSV (`.csv`). Excel includes filters/frozen headers; PDF is formatted as a landscape inventory report.

### Migration 0005
`database/migrations/0005_phase5_asset_tracking.sql` adds two nullable warranty columns. The application startup migration adds the columns automatically when missing. Existing asset data is preserved.

### Final security hardening
- All browser POST forms carry a session-bound CSRF token; POST requests without a valid token are rejected with HTTP 400.
- Session cookies are HTTP-only and use `SameSite=Lax`; `SESSION_COOKIE_SECURE=1` can be enabled when serving the app over HTTPS.
- Real admin credentials are never stored in the repository; configure `ADMIN_EMAIL` and `ADMIN_PASSWORD` in `backend/.env`.
- The real `.env`, uploaded files and generated Python cache files are excluded from the project archive.

### Testing
1. Set warranty provider and expiry on an asset and verify the inventory badge.
2. Verify dashboard warranty counts and the Warranty Alerts page.
3. Open an asset detail page and verify QR display/download.
4. Scan the QR while logged in as admin and verify the correct asset opens.
5. As an employee, verify own-asset QR works and another employee's QR URL returns 404.
6. Export Excel, PDF and CSV and verify the downloaded data.
7. Re-run the Phase 1–4 regression tests after applying migration 0005.
