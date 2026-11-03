"""
Riverview Fire Department - Employee Timesheet Backend API
----------------------------------------------------------
Lightweight Flask application with SQLite / PostgreSQL (Northflank) storage,
CORS configured for riverviewfire.org, Department ID (900-999) + PIN authentication,
retro shift logging with automatic duration calculation, weekly pay period
(Monday 00:00 - Sunday 24:00) aggregation, audit logging, and CFO CSV exports.
"""

import csv
import io
import os
import re
from datetime import datetime, date, timedelta, timezone
from functools import wraps

from flask import Flask, request, jsonify, Response, g
from flask_cors import CORS
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    String,
    Float,
    Boolean,
    Text,
    DateTime,
    ForeignKey,
    func,
)
from sqlalchemy.orm import declarative_base, sessionmaker, scoped_session, relationship
from werkzeug.security import generate_password_hash, check_password_hash

# ==============================================================================
# App & Configuration
# ==============================================================================

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get(
    "SECRET_KEY", "rvfd-timesheet-dev-secret-change-in-production"
)
TOKEN_MAX_AGE_SECONDS = int(os.environ.get("TOKEN_MAX_AGE_SECONDS", 60 * 60 * 12))  # 12h

# Database configuration: defaults to local SQLite, automatically uses PostgreSQL
# when DATABASE_URL is provided (e.g., on Northflank).
raw_db_url = os.environ.get("DATABASE_URL", "").strip()
if raw_db_url:
    if raw_db_url.startswith("postgres://"):
        raw_db_url = raw_db_url.replace("postgres://", "postgresql://", 1)
    DATABASE_URI = raw_db_url
else:
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "timesheet.db")
    DATABASE_URI = f"sqlite:///{db_path}"

# Configure CORS for riverviewfire.org and local development
allowed_origins_env = os.environ.get("CORS_ORIGINS", "")
if allowed_origins_env:
    allowed_origins = [o.strip() for o in allowed_origins_env.split(",") if o.strip()]
else:
    allowed_origins = [
        "https://riverviewfire.org",
        "https://www.riverviewfire.org",
        re.compile(r"^http://localhost(:\d+)?$"),
        re.compile(r"^http://127\.0\.0\.1(:\d+)?$"),
        "null",  # Allows local file:// testing
    ]

CORS(
    app,
    resources={r"/api/*": {"origins": allowed_origins}},
    supports_credentials=True,
    expose_headers=["Content-Disposition"],
)

serializer = URLSafeTimedSerializer(app.config["SECRET_KEY"])

# ==============================================================================
# Database Setup & Models
# ==============================================================================

connect_args = {"check_same_thread": False} if DATABASE_URI.startswith("sqlite") else {}
engine = create_engine(DATABASE_URI, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = scoped_session(sessionmaker(bind=engine, autoflush=False, autocommit=False))
Base = declarative_base()


def utc_now():
    return datetime.now(timezone.utc)


class Employee(Base):
    __tablename__ = "employees"

    dept_id = Column(String(3), primary_key=True)  # "900" through "999"
    name = Column(String(120), nullable=False)
    role = Column(String(20), nullable=False, default="member")  # member | officer | cfo
    pin_hash = Column(String(256), nullable=False)
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), default=utc_now)

    entries = relationship("TimeEntry", back_populates="employee", cascade="all, delete-orphan")

    def to_dict(self):
        return {
            "dept_id": self.dept_id,
            "name": self.name,
            "role": self.role,
            "active": self.active,
            "is_reviewer": self.role in ("officer", "cfo"),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class TimeEntry(Base):
    __tablename__ = "time_entries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    dept_id = Column(String(3), ForeignKey("employees.dept_id"), nullable=False, index=True)
    shift_date = Column(String(10), nullable=False, index=True)  # YYYY-MM-DD
    start_time = Column(String(5), nullable=False)               # HH:MM (00:00 - 23:59)
    end_time = Column(String(5), nullable=False)                 # HH:MM (00:00 - 24:00)
    is_overnight = Column(Boolean, nullable=False, default=False)
    duration_hours = Column(Float, nullable=False)
    pay_period_start = Column(String(10), nullable=False, index=True)  # Monday YYYY-MM-DD
    pay_period_end = Column(String(10), nullable=False, index=True)    # Sunday YYYY-MM-DD
    notes = Column(Text, nullable=True, default="")
    status = Column(String(20), nullable=False, default="pending", index=True)  # pending | approved | rejected
    reviewed_by = Column(String(120), nullable=True)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
    review_note = Column(Text, nullable=True, default="")
    created_at = Column(DateTime(timezone=True), default=utc_now)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    employee = relationship("Employee", back_populates="entries")

    def to_dict(self):
        return {
            "id": self.id,
            "dept_id": self.dept_id,
            "employee_name": self.employee.name if self.employee else f"ID {self.dept_id}",
            "shift_date": self.shift_date,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "is_overnight": self.is_overnight,
            "duration_hours": round(self.duration_hours, 2),
            "pay_period_start": self.pay_period_start,
            "pay_period_end": self.pay_period_end,
            "notes": self.notes or "",
            "status": self.status,
            "reviewed_by": self.reviewed_by,
            "reviewed_at": self.reviewed_at.isoformat() if self.reviewed_at else None,
            "review_note": self.review_note or "",
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime(timezone=True), default=utc_now, index=True)
    actor_id = Column(String(20), nullable=False)
    actor_name = Column(String(120), nullable=False)
    action = Column(String(60), nullable=False)
    target_type = Column(String(40), nullable=False)
    target_id = Column(String(40), nullable=False)
    details = Column(Text, nullable=True)

    def to_dict(self):
        return {
            "id": self.id,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "actor_id": self.actor_id,
            "actor_name": self.actor_name,
            "action": self.action,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "details": self.details or "",
        }


@app.teardown_appcontext
def shutdown_session(exception=None):
    SessionLocal.remove()


# ==============================================================================
# Helper Functions: Validation, Time Math & Pay Periods
# ==============================================================================

DEPT_ID_REGEX = re.compile(r"^9\d{2}$")  # Matches 900 - 999
TIME_REGEX = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$|^24:00$")


def is_valid_dept_id(dept_id: str) -> bool:
    return bool(dept_id and DEPT_ID_REGEX.match(str(dept_id).strip()))


def parse_shift_date(date_str: str) -> date:
    return datetime.strptime(date_str.strip(), "%Y-%m-%d").date()


def get_weekly_pay_period(d: date) -> tuple[str, str]:
    """
    Weekly pay period runs Monday 00:00 through Sunday 24:00
    (ends at midnight between Sunday and Monday).
    """
    monday = d - timedelta(days=d.weekday())  # weekday(): Monday is 0, Sunday is 6
    sunday = monday + timedelta(days=6)
    return monday.isoformat(), sunday.isoformat()


def time_to_minutes(t_str: str) -> int:
    t_str = t_str.strip()
    if not TIME_REGEX.match(t_str):
        raise ValueError(f"Invalid time format '{t_str}'. Use HH:MM (24-hour).")
    if t_str == "24:00":
        return 24 * 60
    hh, mm = map(int, t_str.split(":"))
    return hh * 60 + mm


def calculate_duration(start_time: str, end_time: str) -> tuple[float, bool]:
    """
    Calculates duration in hours between start_time and end_time.
    If end_time <= start_time (and end_time != 24:00), treats it as an overnight
    shift ending the following day.
    Returns (duration_hours, is_overnight).
    """
    start_mins = time_to_minutes(start_time)
    end_mins = time_to_minutes(end_time)

    if start_mins == 24 * 60:
        raise ValueError("Start time cannot be 24:00. Use 00:00 for midnight start.")

    is_overnight = False
    if end_mins <= start_mins:
        # Crosses midnight into the next day
        is_overnight = True
        diff_mins = (24 * 60 - start_mins) + end_mins
    else:
        diff_mins = end_mins - start_mins

    if diff_mins <= 0 or diff_mins > 24 * 60:
        raise ValueError("Shift duration must be between 1 minute and 24 hours.")

    duration_hours = round(diff_mins / 60.0, 2)
    return duration_hours, is_overnight


def log_audit(session, actor_id: str, actor_name: str, action: str, target_type: str, target_id: str, details: str = ""):
    entry = AuditLog(
        actor_id=str(actor_id),
        actor_name=actor_name,
        action=action,
        target_type=target_type,
        target_id=str(target_id),
        details=details,
    )
    session.add(entry)


# ==============================================================================
# Database Initialization & Default Seed
# ==============================================================================

def init_db():
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        existing_count = session.query(Employee).count()
        if existing_count == 0:
            default_admin_pin = os.environ.get("INITIAL_ADMIN_PIN", "1234")
            default_member_hash = generate_password_hash("1234")
            admin_hash = generate_password_hash(default_admin_pin)

            seed_employees = [
                Employee(dept_id="900", name="T. Couch", role="officer", pin_hash=admin_hash, active=True),
                Employee(dept_id="901", name="M. Reynolds", role="cfo", pin_hash=admin_hash, active=True),
                Employee(dept_id="902", name="J. Miller", role="officer", pin_hash=default_member_hash, active=True),
                Employee(dept_id="903", name="S. Davis", role="officer", pin_hash=default_member_hash, active=True),
                Employee(dept_id="904", name="R. Thompson", role="member", pin_hash=default_member_hash, active=True),
                Employee(dept_id="905", name="K. Patel", role="member", pin_hash=default_member_hash, active=True),
                Employee(dept_id="906", name="B. Henderson", role="member", pin_hash=default_member_hash, active=True),
                Employee(dept_id="907", name="A. Morales", role="member", pin_hash=default_member_hash, active=True),
                Employee(dept_id="908", name="D. Brooks", role="member", pin_hash=default_member_hash, active=True),
                Employee(dept_id="909", name="C. Wallace", role="member", pin_hash=default_member_hash, active=True),
                Employee(dept_id="910", name="H. Nguyen", role="member", pin_hash=default_member_hash, active=False),
            ]
            session.add_all(seed_employees)
            session.flush()

            # Seed sample multi-week shift entries (Current Week & Previous Week)
            today = date.today()
            curr_start_str, curr_end_str = get_weekly_pay_period(today)
            curr_mon = parse_shift_date(curr_start_str)
            prev_mon = curr_mon - timedelta(days=7)
            prev_start_str, prev_end_str = get_weekly_pay_period(prev_mon)
            prev_sun = parse_shift_date(prev_end_str)
            now = utc_now()

            seed_entries = [
                # Previous Week (Approved Payroll Week + Sunday Midnight Split Part 1)
                TimeEntry(
                    dept_id="902",
                    shift_date=prev_mon.isoformat(),
                    start_time="07:00",
                    end_time="17:00",
                    is_overnight=False,
                    duration_hours=10.0,
                    pay_period_start=prev_start_str,
                    pay_period_end=prev_end_str,
                    notes="",
                    status="approved",
                    reviewed_by="900 T. Couch",
                    reviewed_at=now,
                ),
                TimeEntry(
                    dept_id="904",
                    shift_date=(prev_mon + timedelta(days=1)).isoformat(),
                    start_time="07:00",
                    end_time="17:00",
                    is_overnight=False,
                    duration_hours=10.0,
                    pay_period_start=prev_start_str,
                    pay_period_end=prev_end_str,
                    notes="Ladder 1 aerial check",
                    status="approved",
                    reviewed_by="902 J. Miller",
                    reviewed_at=now,
                ),
                TimeEntry(
                    dept_id="905",
                    shift_date=(prev_mon + timedelta(days=2)).isoformat(),
                    start_time="07:00",
                    end_time="17:00",
                    is_overnight=False,
                    duration_hours=10.0,
                    pay_period_start=prev_start_str,
                    pay_period_end=prev_end_str,
                    notes="",
                    status="approved",
                    reviewed_by="902 J. Miller",
                    reviewed_at=now,
                ),
                TimeEntry(
                    dept_id="906",
                    shift_date=(prev_mon + timedelta(days=4)).isoformat(),
                    start_time="19:00",
                    end_time="07:00",
                    is_overnight=True,
                    duration_hours=12.0,
                    pay_period_start=prev_start_str,
                    pay_period_end=prev_end_str,
                    notes="",
                    status="approved",
                    reviewed_by="903 S. Davis",
                    reviewed_at=now,
                ),
                TimeEntry(
                    dept_id="907",
                    shift_date=(prev_mon + timedelta(days=5)).isoformat(),
                    start_time="08:00",
                    end_time="20:00",
                    is_overnight=False,
                    duration_hours=12.0,
                    pay_period_start=prev_start_str,
                    pay_period_end=prev_end_str,
                    notes="Community event medical standby (ATV 1)",
                    status="approved",
                    reviewed_by="900 T. Couch",
                    reviewed_at=now,
                ),
                TimeEntry(
                    dept_id="904",
                    shift_date=prev_sun.isoformat(),
                    start_time="20:00",
                    end_time="24:00",
                    is_overnight=False,
                    duration_hours=4.0,
                    pay_period_start=prev_start_str,
                    pay_period_end=prev_end_str,
                    notes="Engine 1 overnight coverage [Part 1/2 - Split at Sun Midnight]",
                    status="approved",
                    reviewed_by="900 T. Couch",
                    reviewed_at=now,
                ),
                # Current Week (Approved, Pending, Sunday Midnight Split Part 2, and Rejected example)
                TimeEntry(
                    dept_id="904",
                    shift_date=curr_mon.isoformat(),
                    start_time="00:00",
                    end_time="06:00",
                    is_overnight=False,
                    duration_hours=6.0,
                    pay_period_start=curr_start_str,
                    pay_period_end=curr_end_str,
                    notes="Engine 1 overnight coverage [Part 2/2 - Split at Sun Midnight]",
                    status="approved",
                    reviewed_by="900 T. Couch",
                    reviewed_at=now,
                ),
                TimeEntry(
                    dept_id="902",
                    shift_date=curr_mon.isoformat(),
                    start_time="07:00",
                    end_time="17:00",
                    is_overnight=False,
                    duration_hours=10.0,
                    pay_period_start=curr_start_str,
                    pay_period_end=curr_end_str,
                    notes="",
                    status="approved",
                    reviewed_by="900 T. Couch",
                    reviewed_at=now,
                ),
                TimeEntry(
                    dept_id="905",
                    shift_date=curr_mon.isoformat(),
                    start_time="07:00",
                    end_time="17:00",
                    is_overnight=False,
                    duration_hours=10.0,
                    pay_period_start=curr_start_str,
                    pay_period_end=curr_end_str,
                    notes="Medical inventory check (Squad 1)",
                    status="approved",
                    reviewed_by="902 J. Miller",
                    reviewed_at=now,
                ),
                TimeEntry(
                    dept_id="906",
                    shift_date=curr_mon.isoformat(),
                    start_time="19:00",
                    end_time="07:00",
                    is_overnight=True,
                    duration_hours=12.0,
                    pay_period_start=curr_start_str,
                    pay_period_end=curr_end_str,
                    notes="",
                    status="approved",
                    reviewed_by="903 S. Davis",
                    reviewed_at=now,
                ),
                TimeEntry(
                    dept_id="907",
                    shift_date=(curr_mon + timedelta(days=1)).isoformat(),
                    start_time="07:00",
                    end_time="17:00",
                    is_overnight=False,
                    duration_hours=10.0,
                    pay_period_start=curr_start_str,
                    pay_period_end=curr_end_str,
                    notes="Rescue 1 ALS coverage & 3 medical calls",
                    status="approved",
                    reviewed_by="902 J. Miller",
                    reviewed_at=now,
                ),
                TimeEntry(
                    dept_id="904",
                    shift_date=(curr_mon + timedelta(days=1)).isoformat(),
                    start_time="18:00",
                    end_time="22:00",
                    is_overnight=False,
                    duration_hours=4.0,
                    pay_period_start=curr_start_str,
                    pay_period_end=curr_end_str,
                    notes="Weekly department pump ops & drafting drill (Tanker 1)",
                    status="approved",
                    reviewed_by="902 J. Miller",
                    reviewed_at=now,
                ),
                TimeEntry(
                    dept_id="908",
                    shift_date=(curr_mon + timedelta(days=1)).isoformat(),
                    start_time="18:00",
                    end_time="22:00",
                    is_overnight=False,
                    duration_hours=4.0,
                    pay_period_start=curr_start_str,
                    pay_period_end=curr_end_str,
                    notes="",
                    status="approved",
                    reviewed_by="902 J. Miller",
                    reviewed_at=now,
                ),
                TimeEntry(
                    dept_id="909",
                    shift_date=(curr_mon + timedelta(days=1)).isoformat(),
                    start_time="18:00",
                    end_time="22:00",
                    is_overnight=False,
                    duration_hours=4.0,
                    pay_period_start=curr_start_str,
                    pay_period_end=curr_end_str,
                    notes="SCBA & ladder evolution drill",
                    status="approved",
                    reviewed_by="903 S. Davis",
                    reviewed_at=now,
                ),
                TimeEntry(
                    dept_id="905",
                    shift_date=(curr_mon + timedelta(days=2)).isoformat(),
                    start_time="02:15",
                    end_time="05:45",
                    is_overnight=False,
                    duration_hours=3.5,
                    pay_period_start=curr_start_str,
                    pay_period_end=curr_end_str,
                    notes="Emergency call-back: Mutual aid structure fire standby",
                    status="pending",
                ),
                TimeEntry(
                    dept_id="904",
                    shift_date=(curr_mon + timedelta(days=3)).isoformat(),
                    start_time="07:00",
                    end_time="17:00",
                    is_overnight=False,
                    duration_hours=10.0,
                    pay_period_start=curr_start_str,
                    pay_period_end=curr_end_str,
                    notes="",
                    status="pending",
                ),
                TimeEntry(
                    dept_id="906",
                    shift_date=(curr_mon + timedelta(days=3)).isoformat(),
                    start_time="13:00",
                    end_time="21:30",
                    is_overnight=False,
                    duration_hours=8.5,
                    pay_period_start=curr_start_str,
                    pay_period_end=curr_end_str,
                    notes="Brush/Grass 1 pump maintenance",
                    status="pending",
                ),
                TimeEntry(
                    dept_id="907",
                    shift_date=(curr_mon + timedelta(days=4)).isoformat(),
                    start_time="19:00",
                    end_time="07:00",
                    is_overnight=True,
                    duration_hours=12.0,
                    pay_period_start=curr_start_str,
                    pay_period_end=curr_end_str,
                    notes="",
                    status="pending",
                ),
                TimeEntry(
                    dept_id="908",
                    shift_date=(curr_mon + timedelta(days=4)).isoformat(),
                    start_time="07:00",
                    end_time="17:00",
                    is_overnight=False,
                    duration_hours=10.0,
                    pay_period_start=curr_start_str,
                    pay_period_end=curr_end_str,
                    notes="Elementary school fire safety demo",
                    status="pending",
                ),
                TimeEntry(
                    dept_id="908",
                    shift_date=(curr_mon + timedelta(days=1)).isoformat(),
                    start_time="18:00",
                    end_time="22:00",
                    is_overnight=False,
                    duration_hours=4.0,
                    pay_period_start=curr_start_str,
                    pay_period_end=curr_end_str,
                    notes="",
                    status="rejected",
                    reviewed_by="902 J. Miller",
                    reviewed_at=now,
                    review_note="Duplicate entry — already approved.",
                ),
            ]
            session.add_all(seed_entries)

            log_audit(
                session,
                actor_id="SYSTEM",
                actor_name="System Initializer",
                action="INITIAL_SEED",
                target_type="employee",
                target_id="900-910",
                details="Created initial roster accounts (900-910) and multi-week sample shift entries.",
            )
            session.commit()
    except Exception as exc:
        session.rollback()
        print(f"Database seed warning: {exc}")
    finally:
        session.close()


init_db()


# ==============================================================================
# Authentication Middleware
# ==============================================================================

def create_auth_token(employee: Employee) -> str:
    return serializer.dumps({"dept_id": employee.dept_id, "role": employee.role})


def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        token = None
        if auth_header.startswith("Bearer "):
            token = auth_header.split(" ", 1)[1].strip()
        elif request.args.get("token"):
            token = request.args.get("token").strip()

        if not token:
            return jsonify({"error": "Authentication required. Please log in."}), 401

        try:
            payload = serializer.loads(token, max_age=TOKEN_MAX_AGE_SECONDS)
        except SignatureExpired:
            return jsonify({"error": "Session expired. Please log in again."}), 401
        except BadSignature:
            return jsonify({"error": "Invalid authentication token."}), 401

        session = SessionLocal()
        emp = session.query(Employee).filter_by(dept_id=payload.get("dept_id")).first()
        if not emp or not emp.active:
            return jsonify({"error": "Account is inactive or no longer exists."}), 403

        g.current_user = emp
        g.db = session
        return f(*args, **kwargs)

    return decorated


def require_reviewer(f):
    @wraps(f)
    @require_auth
    def decorated(*args, **kwargs):
        if g.current_user.role not in ("officer", "cfo"):
            return jsonify({"error": "Officer or CFO permissions required."}), 403
        return f(*args, **kwargs)

    return decorated


# ==============================================================================
# API Routes: Health & Authentication
# ==============================================================================

@app.route("/api/health", methods=["GET"])
def health_check():
    today = date.today()
    pp_start, pp_end = get_weekly_pay_period(today)
    return jsonify({
        "status": "ok",
        "service": "Riverview FD Timesheet API",
        "current_pay_period": {
            "start": pp_start,
            "end": pp_end,
        },
    })


@app.route("/api/auth/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    dept_id = str(data.get("dept_id", "")).strip()
    pin = str(data.get("pin", "")).strip()

    if not is_valid_dept_id(dept_id):
        return jsonify({"error": "Department ID must be between 900 and 999."}), 400
    if not pin:
        return jsonify({"error": "Please enter your PIN / password."}), 400

    session = SessionLocal()
    emp = session.query(Employee).filter_by(dept_id=dept_id).first()
    if not emp or not check_password_hash(emp.pin_hash, pin):
        return jsonify({"error": "Invalid Department ID or PIN."}), 401

    if not emp.active:
        return jsonify({"error": "This Department ID is currently inactive. Contact an officer."}), 403

    token = create_auth_token(emp)
    return jsonify({
        "token": token,
        "user": emp.to_dict(),
    })


@app.route("/api/auth/me", methods=["GET"])
@require_auth
def get_me():
    return jsonify({"user": g.current_user.to_dict()})


@app.route("/api/auth/change-pin", methods=["POST"])
@require_auth
def change_own_pin():
    data = request.get_json(silent=True) or {}
    current_pin = str(data.get("current_pin", "")).strip()
    new_pin = str(data.get("new_pin", "")).strip()

    if not current_pin or not new_pin:
        return jsonify({"error": "Both current PIN and new PIN are required."}), 400
    if len(new_pin) < 4:
        return jsonify({"error": "New PIN/password must be at least 4 characters."}), 400

    emp = g.current_user
    if not check_password_hash(emp.pin_hash, current_pin):
        return jsonify({"error": "Current PIN is incorrect."}), 400

    emp.pin_hash = generate_password_hash(new_pin)
    log_audit(
        g.db,
        actor_id=emp.dept_id,
        actor_name=emp.name,
        action="PIN_CHANGED",
        target_type="employee",
        target_id=emp.dept_id,
        details="Member updated their own PIN.",
    )
    g.db.commit()
    return jsonify({"message": "PIN updated successfully."})


# ==============================================================================
# API Routes: Time Entries (Retro Logging & Officer Review)
# ==============================================================================

@app.route("/api/entries", methods=["GET"])
@require_auth
def list_entries():
    emp = g.current_user
    is_reviewer = emp.role in ("officer", "cfo")

    query = g.db.query(TimeEntry)

    # Regular members only see their own entries
    if not is_reviewer or request.args.get("mine") == "true":
        query = query.filter(TimeEntry.dept_id == emp.dept_id)
    else:
        filter_dept_id = request.args.get("dept_id", "").strip()
        if filter_dept_id:
            query = query.filter(TimeEntry.dept_id == filter_dept_id)

    pay_period_start = request.args.get("pay_period_start", "").strip()
    if pay_period_start:
        query = query.filter(TimeEntry.pay_period_start == pay_period_start)

    start_date = request.args.get("start_date", "").strip()
    if start_date:
        query = query.filter(TimeEntry.shift_date >= start_date)

    end_date = request.args.get("end_date", "").strip()
    if end_date:
        query = query.filter(TimeEntry.shift_date <= end_date)

    status = request.args.get("status", "").strip()
    if status and status != "all":
        query = query.filter(TimeEntry.status == status)

    entries = query.order_by(TimeEntry.shift_date.desc(), TimeEntry.start_time.desc(), TimeEntry.id.desc()).all()
    return jsonify({
        "entries": [e.to_dict() for e in entries]
    })


@app.route("/api/entries", methods=["POST"])
@require_auth
def create_entry():
    data = request.get_json(silent=True) or {}
    emp = g.current_user
    is_reviewer = emp.role in ("officer", "cfo")

    # Officers can log on behalf of another member if dept_id is provided
    target_dept_id = str(data.get("dept_id") or emp.dept_id).strip()
    if target_dept_id != emp.dept_id and not is_reviewer:
        return jsonify({"error": "You can only submit hours for your own Department ID."}), 403

    target_emp = g.db.query(Employee).filter_by(dept_id=target_dept_id).first()
    if not target_emp:
        return jsonify({"error": f"Department ID {target_dept_id} not found."}), 404

    shift_date_str = str(data.get("shift_date", "")).strip()
    start_time_str = str(data.get("start_time", "")).strip()
    end_time_str = str(data.get("end_time", "")).strip()
    notes = str(data.get("notes", "")).strip()
    split_sunday_midnight = bool(data.get("split_sunday_midnight", True))

    if not shift_date_str or not start_time_str or not end_time_str:
        return jsonify({"error": "Shift date, start time, and end time are required."}), 400

    try:
        shift_dt = parse_shift_date(shift_date_str)
        duration_hours, is_overnight = calculate_duration(start_time_str, end_time_str)
    except ValueError as err:
        return jsonify({"error": str(err)}), 400

    created_entries = []

    # Pay period ends at midnight between Sunday and Monday.
    # If a shift starts on Sunday (weekday == 6) and crosses midnight into Monday,
    # automatically split at 24:00 / 00:00 if split_sunday_midnight is enabled.
    if shift_dt.weekday() == 6 and is_overnight and end_time_str != "00:00" and split_sunday_midnight:
        # Part 1: Sunday start_time -> 24:00 (belongs to Sunday's pay period)
        sun_hours, _ = calculate_duration(start_time_str, "24:00")
        sun_pp_start, sun_pp_end = get_weekly_pay_period(shift_dt)
        entry_sun = TimeEntry(
            dept_id=target_dept_id,
            shift_date=shift_dt.isoformat(),
            start_time=start_time_str,
            end_time="24:00",
            is_overnight=False,
            duration_hours=sun_hours,
            pay_period_start=sun_pp_start,
            pay_period_end=sun_pp_end,
            notes=f"{notes} [Part 1/2 - Split at Sun Midnight]".strip(),
            status="pending",
        )
        g.db.add(entry_sun)

        # Part 2: Monday 00:00 -> end_time (belongs to new Monday pay period)
        mon_dt = shift_dt + timedelta(days=1)
        mon_hours, _ = calculate_duration("00:00", end_time_str)
        mon_pp_start, mon_pp_end = get_weekly_pay_period(mon_dt)
        entry_mon = TimeEntry(
            dept_id=target_dept_id,
            shift_date=mon_dt.isoformat(),
            start_time="00:00",
            end_time=end_time_str,
            is_overnight=False,
            duration_hours=mon_hours,
            pay_period_start=mon_pp_start,
            pay_period_end=mon_pp_end,
            notes=f"{notes} [Part 2/2 - Split at Sun Midnight]".strip(),
            status="pending",
        )
        g.db.add(entry_mon)
        g.db.flush()
        created_entries = [entry_sun, entry_mon]

        log_audit(
            g.db,
            actor_id=emp.dept_id,
            actor_name=emp.name,
            action="ENTRY_CREATED_SPLIT",
            target_type="time_entry",
            target_id=f"{entry_sun.id},{entry_mon.id}",
            details=(
                f"Logged overnight Sunday shift for {target_dept_id} ({shift_date_str} {start_time_str}-{end_time_str}), "
                f"split at Sunday midnight into {sun_hours}h and {mon_hours}h."
            ),
        )
    else:
        pp_start, pp_end = get_weekly_pay_period(shift_dt)
        entry = TimeEntry(
            dept_id=target_dept_id,
            shift_date=shift_dt.isoformat(),
            start_time=start_time_str,
            end_time=end_time_str,
            is_overnight=is_overnight,
            duration_hours=duration_hours,
            pay_period_start=pp_start,
            pay_period_end=pp_end,
            notes=notes,
            status="pending",
        )
        g.db.add(entry)
        g.db.flush()
        created_entries = [entry]

        log_audit(
            g.db,
            actor_id=emp.dept_id,
            actor_name=emp.name,
            action="ENTRY_CREATED",
            target_type="time_entry",
            target_id=str(entry.id),
            details=(
                f"Logged {duration_hours}h for {target_dept_id} on {shift_date_str} "
                f"({start_time_str} - {end_time_str})."
            ),
        )

    g.db.commit()
    return jsonify({
        "message": "Hours logged successfully.",
        "entries": [e.to_dict() for e in created_entries],
    }), 201


@app.route("/api/entries/<int:entry_id>", methods=["PUT"])
@require_auth
def update_entry(entry_id: int):
    emp = g.current_user
    is_reviewer = emp.role in ("officer", "cfo")

    entry = g.db.query(TimeEntry).filter_by(id=entry_id).first()
    if not entry:
        return jsonify({"error": "Time entry not found."}), 404

    if not is_reviewer:
        if entry.dept_id != emp.dept_id:
            return jsonify({"error": "You can only edit your own entries."}), 403
        if entry.status != "pending":
            return jsonify({"error": "Only pending entries can be edited by members."}), 400

    data = request.get_json(silent=True) or {}
    shift_date_str = str(data.get("shift_date", entry.shift_date)).strip()
    start_time_str = str(data.get("start_time", entry.start_time)).strip()
    end_time_str = str(data.get("end_time", entry.end_time)).strip()
    notes = str(data.get("notes", entry.notes or "")).strip()

    try:
        shift_dt = parse_shift_date(shift_date_str)
        duration_hours, is_overnight = calculate_duration(start_time_str, end_time_str)
    except ValueError as err:
        return jsonify({"error": str(err)}), 400

    pp_start, pp_end = get_weekly_pay_period(shift_dt)

    old_summary = f"{entry.shift_date} {entry.start_time}-{entry.end_time} ({entry.duration_hours}h)"
    entry.shift_date = shift_dt.isoformat()
    entry.start_time = start_time_str
    entry.end_time = end_time_str
    entry.is_overnight = is_overnight
    entry.duration_hours = duration_hours
    entry.pay_period_start = pp_start
    entry.pay_period_end = pp_end
    entry.notes = notes
    entry.updated_at = utc_now()

    new_summary = f"{entry.shift_date} {entry.start_time}-{entry.end_time} ({entry.duration_hours}h)"
    log_audit(
        g.db,
        actor_id=emp.dept_id,
        actor_name=emp.name,
        action="ENTRY_UPDATED",
        target_type="time_entry",
        target_id=str(entry.id),
        details=f"Updated entry {entry.id} for {entry.dept_id}: {old_summary} -> {new_summary}",
    )
    g.db.commit()
    return jsonify({"message": "Entry updated.", "entry": entry.to_dict()})


@app.route("/api/entries/<int:entry_id>", methods=["DELETE"])
@require_auth
def delete_entry(entry_id: int):
    emp = g.current_user
    is_reviewer = emp.role in ("officer", "cfo")

    entry = g.db.query(TimeEntry).filter_by(id=entry_id).first()
    if not entry:
        return jsonify({"error": "Time entry not found."}), 404

    if not is_reviewer:
        if entry.dept_id != emp.dept_id:
            return jsonify({"error": "You can only delete your own entries."}), 403
        if entry.status != "pending":
            return jsonify({"error": "Only pending entries can be deleted by members."}), 400

    details = f"Deleted entry {entry.id} for {entry.dept_id} ({entry.shift_date} {entry.start_time}-{entry.end_time}, {entry.duration_hours}h)"
    g.db.delete(entry)
    log_audit(
        g.db,
        actor_id=emp.dept_id,
        actor_name=emp.name,
        action="ENTRY_DELETED",
        target_type="time_entry",
        target_id=str(entry_id),
        details=details,
    )
    g.db.commit()
    return jsonify({"message": "Time entry deleted."})


@app.route("/api/entries/<int:entry_id>/review", methods=["POST"])
@require_reviewer
def review_entry(entry_id: int):
    emp = g.current_user
    data = request.get_json(silent=True) or {}
    new_status = str(data.get("status", "")).strip().lower()
    review_note = str(data.get("review_note", "")).strip()

    if new_status not in ("approved", "rejected", "pending"):
        return jsonify({"error": "Status must be 'approved', 'rejected', or 'pending'."}), 400

    entry = g.db.query(TimeEntry).filter_by(id=entry_id).first()
    if not entry:
        return jsonify({"error": "Time entry not found."}), 404

    entry.status = new_status
    entry.reviewed_by = f"{emp.dept_id} {emp.name}"
    entry.reviewed_at = utc_now()
    entry.review_note = review_note

    log_audit(
        g.db,
        actor_id=emp.dept_id,
        actor_name=emp.name,
        action=f"ENTRY_{new_status.upper()}",
        target_type="time_entry",
        target_id=str(entry.id),
        details=(
            f"Marked entry {entry.id} ({entry.dept_id}, {entry.shift_date}, {entry.duration_hours}h) "
            f"as {new_status}." + (f" Note: {review_note}" if review_note else "")
        ),
    )
    g.db.commit()
    return jsonify({"message": f"Entry {new_status}.", "entry": entry.to_dict()})


@app.route("/api/entries/bulk-approve", methods=["POST"])
@require_reviewer
def bulk_approve():
    emp = g.current_user
    data = request.get_json(silent=True) or {}
    pay_period_start = str(data.get("pay_period_start", "")).strip()
    dept_id = str(data.get("dept_id", "")).strip()

    query = g.db.query(TimeEntry).filter(TimeEntry.status == "pending")
    if pay_period_start:
        query = query.filter(TimeEntry.pay_period_start == pay_period_start)
    if dept_id:
        query = query.filter(TimeEntry.dept_id == dept_id)

    pending_entries = query.all()
    if not pending_entries:
        return jsonify({"message": "No pending entries found for the selected filter.", "approved_count": 0})

    now = utc_now()
    ids = []
    for entry in pending_entries:
        entry.status = "approved"
        entry.reviewed_by = f"{emp.dept_id} {emp.name}"
        entry.reviewed_at = now
        ids.append(str(entry.id))

    log_audit(
        g.db,
        actor_id=emp.dept_id,
        actor_name=emp.name,
        action="BULK_APPROVED",
        target_type="time_entry",
        target_id=f"{len(ids)} entries",
        details=f"Bulk approved {len(ids)} pending entries (Pay Period: {pay_period_start or 'All'}).",
    )
    g.db.commit()
    return jsonify({
        "message": f"Approved {len(ids)} pending entries.",
        "approved_count": len(ids),
    })


# ==============================================================================
# API Routes: CFO Pay Period Summary & CSV Export
# ==============================================================================

def build_payroll_summary(session, pay_period_start: str = "", start_date: str = "", end_date: str = ""):
    """
    Aggregates hours by employee for a given weekly pay period (Monday-Sunday)
    or custom date range.
    """
    query = session.query(TimeEntry)
    if pay_period_start:
        query = query.filter(TimeEntry.pay_period_start == pay_period_start)
        pp_dt = parse_shift_date(pay_period_start)
        period_start_str, period_end_str = get_weekly_pay_period(pp_dt)
    elif start_date and end_date:
        query = query.filter(TimeEntry.shift_date >= start_date, TimeEntry.shift_date <= end_date)
        period_start_str, period_end_str = start_date, end_date
    else:
        today = date.today()
        period_start_str, period_end_str = get_weekly_pay_period(today)
        query = query.filter(TimeEntry.pay_period_start == period_start_str)

    entries = query.order_by(TimeEntry.dept_id.asc(), TimeEntry.shift_date.asc(), TimeEntry.start_time.asc()).all()
    employees = {e.dept_id: e for e in session.query(Employee).order_by(Employee.dept_id.asc()).all()}

    by_employee = {}
    for e in entries:
        emp_obj = employees.get(e.dept_id)
        if e.dept_id not in by_employee:
            by_employee[e.dept_id] = {
                "dept_id": e.dept_id,
                "name": emp_obj.name if emp_obj else f"ID {e.dept_id}",
                "role": emp_obj.role if emp_obj else "member",
                "approved_hours": 0.0,
                "pending_hours": 0.0,
                "rejected_hours": 0.0,
                "total_submitted_hours": 0.0,
                "shift_count": 0,
                "approved_shift_count": 0,
            }
        rec = by_employee[e.dept_id]
        rec["shift_count"] += 1
        rec["total_submitted_hours"] = round(rec["total_submitted_hours"] + e.duration_hours, 2)
        if e.status == "approved":
            rec["approved_hours"] = round(rec["approved_hours"] + e.duration_hours, 2)
            rec["approved_shift_count"] += 1
        elif e.status == "pending":
            rec["pending_hours"] = round(rec["pending_hours"] + e.duration_hours, 2)
        elif e.status == "rejected":
            rec["rejected_hours"] = round(rec["rejected_hours"] + e.duration_hours, 2)

    summary_rows = sorted(by_employee.values(), key=lambda r: r["dept_id"])

    totals = {
        "employee_count": len(summary_rows),
        "total_shifts": sum(r["shift_count"] for r in summary_rows),
        "approved_shifts": sum(r["approved_shift_count"] for r in summary_rows),
        "approved_hours": round(sum(r["approved_hours"] for r in summary_rows), 2),
        "pending_hours": round(sum(r["pending_hours"] for r in summary_rows), 2),
        "rejected_hours": round(sum(r["rejected_hours"] for r in summary_rows), 2),
        "total_submitted_hours": round(sum(r["total_submitted_hours"] for r in summary_rows), 2),
    }

    return {
        "pay_period_start": period_start_str,
        "pay_period_end": period_end_str,
        "summary": summary_rows,
        "totals": totals,
        "entries": [e.to_dict() for e in entries],
    }


@app.route("/api/summary", methods=["GET"])
@require_reviewer
def get_summary():
    pay_period_start = request.args.get("pay_period_start", "").strip()
    start_date = request.args.get("start_date", "").strip()
    end_date = request.args.get("end_date", "").strip()
    try:
        data = build_payroll_summary(g.db, pay_period_start, start_date, end_date)
    except ValueError as err:
        return jsonify({"error": str(err)}), 400
    return jsonify(data)


@app.route("/api/export/csv", methods=["GET"])
@require_reviewer
def export_csv():
    export_type = request.args.get("type", "summary").strip().lower()
    pay_period_start = request.args.get("pay_period_start", "").strip()
    start_date = request.args.get("start_date", "").strip()
    end_date = request.args.get("end_date", "").strip()
    approved_only = request.args.get("approved_only", "false").lower() == "true"

    try:
        data = build_payroll_summary(g.db, pay_period_start, start_date, end_date)
    except ValueError as err:
        return jsonify({"error": str(err)}), 400

    p_start = data["pay_period_start"]
    p_end = data["pay_period_end"]

    output = io.StringIO()
    writer = csv.writer(output)

    if export_type == "detailed":
        writer.writerow([
            "Pay Period Start (Mon)",
            "Pay Period End (Sun)",
            "Department ID",
            "Employee Name",
            "Shift Date",
            "Start Time",
            "End Time",
            "Overnight",
            "Duration (Hours)",
            "Status",
            "Reviewed By",
            "Reviewed At (UTC)",
            "Notes",
            "Review Note",
        ])
        for e in data["entries"]:
            if approved_only and e["status"] != "approved":
                continue
            writer.writerow([
                e["pay_period_start"],
                e["pay_period_end"],
                e["dept_id"],
                e["employee_name"],
                e["shift_date"],
                e["start_time"],
                e["end_time"],
                "Yes" if e["is_overnight"] else "No",
                f"{e['duration_hours']:.2f}",
                e["status"].upper(),
                e["reviewed_by"] or "",
                e["reviewed_at"] or "",
                e["notes"] or "",
                e["review_note"] or "",
            ])
        filename = f"RVFD_Timesheet_Detailed_{p_start}_to_{p_end}.csv"
    else:
        # CFO Payroll Summary CSV
        writer.writerow([
            "Pay Period Start (Mon)",
            "Pay Period End (Sun)",
            "Department ID",
            "Employee Name",
            "Role",
            "Approved Hours (Payroll)",
            "Pending Hours",
            "Rejected Hours",
            "Total Submitted Hours",
            "Approved Shifts",
            "Total Shifts",
        ])
        for row in data["summary"]:
            if approved_only and row["approved_hours"] <= 0:
                continue
            writer.writerow([
                p_start,
                p_end,
                row["dept_id"],
                row["name"],
                row["role"].upper(),
                f"{row['approved_hours']:.2f}",
                f"{row['pending_hours']:.2f}",
                f"{row['rejected_hours']:.2f}",
                f"{row['total_submitted_hours']:.2f}",
                row["approved_shift_count"],
                row["shift_count"],
            ])
        # Totals footer row for CFO convenience
        t = data["totals"]
        writer.writerow([])
        writer.writerow([
            p_start,
            p_end,
            "TOTAL",
            f"{t['employee_count']} Employees",
            "",
            f"{t['approved_hours']:.2f}",
            f"{t['pending_hours']:.2f}",
            f"{t['rejected_hours']:.2f}",
            f"{t['total_submitted_hours']:.2f}",
            t["approved_shifts"],
            t["total_shifts"],
        ])
        filename = f"RVFD_CFO_Payroll_Summary_{p_start}_to_{p_end}.csv"

    csv_content = output.getvalue()
    output.close()

    return Response(
        csv_content,
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ==============================================================================
# API Routes: Employee Roster Management (900-999) & Audit Trail
# ==============================================================================

@app.route("/api/employees", methods=["GET"])
@require_reviewer
def list_employees():
    employees = g.db.query(Employee).order_by(Employee.dept_id.asc()).all()
    return jsonify({"employees": [e.to_dict() for e in employees]})


@app.route("/api/employees", methods=["POST"])
@require_reviewer
def create_employee():
    actor = g.current_user
    data = request.get_json(silent=True) or {}
    dept_id = str(data.get("dept_id", "")).strip()
    name = str(data.get("name", "")).strip()
    role = str(data.get("role", "member")).strip().lower()
    pin = str(data.get("pin", "")).strip()

    if not is_valid_dept_id(dept_id):
        return jsonify({"error": "Department ID must be a 3-digit number between 900 and 999."}), 400
    if not name:
        return jsonify({"error": "Employee name is required."}), 400
    if role not in ("member", "officer", "cfo"):
        return jsonify({"error": "Role must be 'member', 'officer', or 'cfo'."}), 400
    if len(pin) < 4:
        return jsonify({"error": "Initial PIN/password must be at least 4 characters."}), 400

    existing = g.db.query(Employee).filter_by(dept_id=dept_id).first()
    if existing:
        return jsonify({"error": f"Department ID {dept_id} is already assigned to {existing.name}."}), 409

    new_emp = Employee(
        dept_id=dept_id,
        name=name,
        role=role,
        pin_hash=generate_password_hash(pin),
        active=True,
    )
    g.db.add(new_emp)
    log_audit(
        g.db,
        actor_id=actor.dept_id,
        actor_name=actor.name,
        action="EMPLOYEE_CREATED",
        target_type="employee",
        target_id=dept_id,
        details=f"Added {dept_id} ({name}) with role '{role}'.",
    )
    g.db.commit()
    return jsonify({"message": f"Added {dept_id} ({name}).", "employee": new_emp.to_dict()}), 201


@app.route("/api/employees/<dept_id>", methods=["PUT"])
@require_reviewer
def update_employee(dept_id: str):
    actor = g.current_user
    emp = g.db.query(Employee).filter_by(dept_id=dept_id.strip()).first()
    if not emp:
        return jsonify({"error": f"Department ID {dept_id} not found."}), 404

    data = request.get_json(silent=True) or {}
    changes = []

    if "name" in data and str(data["name"]).strip():
        new_name = str(data["name"]).strip()
        if new_name != emp.name:
            changes.append(f"name '{emp.name}' -> '{new_name}'")
            emp.name = new_name

    if "role" in data and str(data["role"]).strip().lower() in ("member", "officer", "cfo"):
        new_role = str(data["role"]).strip().lower()
        if new_role != emp.role:
            changes.append(f"role '{emp.role}' -> '{new_role}'")
            emp.role = new_role

    if "active" in data:
        new_active = bool(data["active"])
        if new_active != emp.active:
            changes.append(f"active {emp.active} -> {new_active}")
            emp.active = new_active

    if "pin" in data and str(data["pin"]).strip():
        new_pin = str(data["pin"]).strip()
        if len(new_pin) < 4:
            return jsonify({"error": "PIN/password must be at least 4 characters."}), 400
        emp.pin_hash = generate_password_hash(new_pin)
        changes.append("PIN reset")

    if changes:
        log_audit(
            g.db,
            actor_id=actor.dept_id,
            actor_name=actor.name,
            action="EMPLOYEE_UPDATED",
            target_type="employee",
            target_id=emp.dept_id,
            details=f"Updated {emp.dept_id} ({emp.name}): {', '.join(changes)}.",
        )
        g.db.commit()

    return jsonify({"message": f"Updated {emp.dept_id}.", "employee": emp.to_dict()})


@app.route("/api/audit-logs", methods=["GET"])
@require_reviewer
def get_audit_logs():
    limit = min(int(request.args.get("limit", 100)), 500)
    logs = g.db.query(AuditLog).order_by(AuditLog.timestamp.desc(), AuditLog.id.desc()).limit(limit).all()
    return jsonify({"logs": [l.to_dict() for l in logs]})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
