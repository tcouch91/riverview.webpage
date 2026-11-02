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

---

## Local Development

```powershell
cd timesheet_backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

The API will start on `http://127.0.0.1:5000`. Open `timesheet.html` in your browser.

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
3. Once deployed, copy your Northflank service URL (e.g. `https://site--timesheet--xxxx.code.run`) into `CONFIG.PROD_BACKEND_URL` in `timesheet.html`.
