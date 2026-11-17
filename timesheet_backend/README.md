# Riverview Fire Department — Employee Timesheet Backend

Lightweight Flask backend for the Riverview Fire Department retro timesheet & CFO payroll portal (`timesheet.html`).

## Features
- **Department ID Authentication (`900–999` + PIN/Password)**:
  - Hashed PINs using Werkzeug security and signed session tokens via `itsdangerous`.
  - Roles: `member` (log & manage own pending shifts), `officer` (approve/reject shifts, manage roster, view audit trail), and `cfo` (payroll summaries, CSV/PDF exports, review access).
- **Retro Shift Logging with Auto-Calculated Duration**:
  - Calculates duration automatically from `Start Time` and `End Time` (supports overnight shifts crossing midnight).
  - **Weekly Pay Period Cutoff (Sunday 24:00 / Monday 00:00)**: Automatically groups shifts into `Monday 00:00 – Sunday 24:00` weekly pay periods and splits overnight Sunday shifts at midnight across the two pay periods when enabled.
- **Storage**:
  - Uses **SQLite** (`timesheet.db`) automatically out of the box for local use.
  - Automatically switches to **PostgreSQL** when `DATABASE_URL` is set (e.g., on Northflank).
- **CFO Exports**:
  - One-click **CFO Payroll Summary CSV** (aggregated by employee for the selected Monday–Sunday week).
  - One-click **Detailed Shift Audit CSV** (every individual shift record with review timestamps).
  - Printable / Save-as-PDF formal **Weekly Payroll Timesheet Summary & Sign-Off Report** in `timesheet.html`.
- **Monthly Scheduling** (Schedule side of the portal, `js/schedule.js` + `css/schedule.css`):
  - **Availability (hourly)**: members click and drag across a When2meet-style grid (1-hour cells, one week at a time) for an upcoming month. Each day runs from the first shift's start (07:00) to 07:00 the next morning, so a night shift is one straight drag. Quick fills (All Days / All Nights / Copy Week to All), whole-day and whole-hour toggles, plus a max-hours preference and notes. Due 14 days before the month starts by default; officers can move the deadline per month.
  - **Partial availability**: someone available for only part of a shift (e.g. can't start until 08:00) can still fill the seat. The seat counts as **completely filled**, and their hours are the part they work (08:00–17:00 = 9h). It only counts toward a shift if it covers at least `min_coverage_pct` of it (default 75%, editable).
  - **Qualified drivers**: a Driver flag on each member (Roster tab or a `DRIVER` yes/no column in the roster CSV). Positions can require a driver (Station 1 and Station 2 by default). The Builder flags crews without one, and when a crew still lacks a driver its last open seat is held for one: Auto-Fill and member sign-ups won't give it to a non-driver.
  - **Builder** (officers/CFO): every seat is a dropdown listing members available for that shift first (drivers first when the crew needs one, then fewest hours). A **Projected Hours** table shows each member's month total and every Monday–Sunday pay week, flagging overtime against their 40h / 53h rule. Week totals include neighboring months, and overnight Sunday shifts split at midnight like the timesheet does. Double-booking is blocked. **Add / remove seats** per day for special events or out-of-service units.
  - **Auto-Fill** fills open future seats from availability. It puts a driver on each station crew first, then fills the scarcest seats with the fewest-hours members (full coverage preferred over partial). It never double-books, never schedules more than 24h straight, never goes over a member's max hours, and avoids overtime unless "Allow overtime" is checked. Seats already filled aren't touched, and **Undo Auto-Fill** clears only the seats it filled.
  - **Staffing levels** are editable (defaults: Day 07:00–17:00 = Station 1 ×2, Station 2 ×2, Squad ×1; Night 17:00–07:00 = Station 1 ×1, Station 2 ×1).
  - **Publish** makes the month visible; any seat still open becomes a vacancy members can claim (first come, first served). Members can drop their own sign-ups until 24 hours before the shift; officer-assigned shifts can only be changed by an officer.
  - **Timesheet tie-in**: a scheduled shift shows as a one-tap "Scheduled" preset on Log Shift (with partial hours when applicable), and "Log Hours" on the Schedule tab pre-fills the timesheet form.
  - New tables (`schedule_months`, `schedule_slots`, `schedule_availability_hours`, `schedule_submissions`, `schedule_settings`) and the `employees.is_driver` column are created automatically on startup. All schedule changes are written to the audit log.

---

## Local Development

```powershell
cd timesheet_backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

The API will start on `http://127.0.0.1:5000`. Serve the site locally (for example `python -m http.server 8080` from the repo root) and open `http://localhost:8080/timesheet.html`. When the page is served from `localhost`, it uses the local backend first; opened as a `file://` page it tries the production backend first. Once connected, the page stays on that backend for the session: if it goes down, the page shows "Server Issue" instead of switching servers.

### Demo data (fake roster, safe to play with)
```powershell
cd timesheet_backend
.\.venv\Scripts\python demo_data.py --serve    # rebuilds demo.db, then serves the API on :5000
```
Then run `python -m http.server 8080` from the repo root and open `http://localhost:8080/timesheet.html`. Every demo PIN is `1234`: **953** officer (Builder), **961** member who hasn't entered availability yet, **963** member with partial (08:00) availability, **960** full-time driver. `demo.db` is separate from `timesheet.db`, git-ignored, and rebuilt on every run. Dates follow today: this month is published and filled, and next month is open for availability and Auto-Fill. In the Claude desktop app, `.claude/launch.json` starts both servers (`schedule-demo-api` and `site`).

### Scheduling API (summary)
| Method & Path | Who | Purpose |
| :--- | :--- | :--- |
| `GET /api/schedule/<YYYY-MM>` | any | Published seats, your availability, your projected hours, the next availability deadline |
| `PUT /api/schedule/<YYYY-MM>/availability` | any (officers for others) | Replace a member's hourly availability (`{"start_hour": 7, "days": {"2026-12-01": <24-bit mask>}}`), notes, and max hours |
| `GET /api/schedule/<YYYY-MM>/builder` | officer/CFO | All seats, availability, submissions, projected hours, coverage (incl. drivers) |
| `PUT /api/schedule/slots/<id>` | officer/CFO | Assign or clear a seat (`{"dept_id": "961"}` or `null`); partial availability sets the covered hours |
| `POST /api/schedule/<YYYY-MM>/slots` | officer/CFO | Add a seat (`{"shift_date", "shift_key", "position_key"}`) |
| `DELETE /api/schedule/slots/<id>` | officer/CFO | Remove a seat (`?force=true` if it's filled) |
| `POST /api/schedule/<YYYY-MM>/autofill` · `/autofill/clear` | officer/CFO | Auto-fill open seats (`{"allow_ot": false}`) / undo auto-fill |
| `POST /api/schedule/slots/<id>/signup` · `/drop` | any | Claim an open seat / drop your own sign-up |
| `POST /api/schedule/<YYYY-MM>/publish` | officer/CFO | `{"published": true}` to publish, `false` to return to draft |
| `PUT /api/schedule/<YYYY-MM>` | officer/CFO | Change `availability_deadline` / `notes` |
| `GET` · `PUT /api/schedule/settings` | any · officer/CFO | Staffing levels template |
| `POST /api/schedule/<YYYY-MM>/sync` | officer/CFO | Apply the current staffing levels to an existing month |
| `GET /api/schedule/shifts?start=&end=` | any | Your published shifts in a date range (Log Shift presets) |

### Initial Seed Accounts (Created on first startup)
| Dept ID | Role | Default PIN | Description |
| :--- | :--- | :--- | :--- |
| **900** | `officer` | `1234` | Fire Chief / Reviewing Officer |
| **901** | `cfo` | `1234` | Department CFO |
| **902** | `member` | `1234` | Sample Firefighter |
| **903** | `member` | `1234` | Sample Engineer |

*(Officers can add any Department ID from `900` to `999` and change PINs inside the **Roster & IDs** tab.)*

---

## Deploying to Northflank

1. Create a new **Combined Service** (or build from this repo with Build Context set to `/timesheet_backend` and `Dockerfile`).
2. Set Environment Variables:
   - `SECRET_KEY`: A strong random secret string for signing login tokens.
   - `INITIAL_ADMIN_PIN`: Initial PIN for accounts `900` and `901` on first boot.
   - `DATABASE_URL`: Link your Northflank PostgreSQL addon connection string (or mount a persistent volume for SQLite).
   - `CORS_ORIGINS` (optional): Defaults to `https://riverviewfire.org,https://www.riverviewfire.org`.
   - `LOCAL_TIMEZONE` (optional): Time zone for schedule deadlines and sign-up cutoffs. Defaults to `America/New_York`.
   - `SIGNUP_DROP_CUTOFF_HOURS` (optional): How long before a shift members can still drop a sign-up. Defaults to `24`.
   - `SCHEDULE_MAX_CONTINUOUS_HOURS` (optional): Longest back-to-back stretch Auto-Fill will schedule one person. Defaults to `24`.
3. Once deployed, copy your Northflank service URL (e.g. `https://site--timesheet--xxxx.code.run`) into `CONFIG.PROD_BACKEND_URL` in `timesheet.html`.
