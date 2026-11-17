"""
Riverview Fire Department - Employee Timesheet Backend API
----------------------------------------------------------
Lightweight Flask application with SQLite / PostgreSQL (Northflank) storage,
CORS configured for riverviewfire.org, Department ID (900-999) + PIN authentication,
retro shift logging with automatic duration calculation, weekly pay period
(Monday 00:00 - Sunday 24:00) aggregation, audit logging, CFO CSV exports,
and monthly shift scheduling (availability, officer schedule builder with
projected hours, and vacancy sign-ups).
"""

import copy
import csv
import io
import json
import os
import re
from collections import defaultdict
from datetime import datetime, date, time as dt_time, timedelta, timezone
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
    UniqueConstraint,
    text,
)
from sqlalchemy.exc import IntegrityError
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
# when DATABASE_URL or POSTGRES_URI is provided (e.g., on Northflank).
raw_db_url = (
    os.environ.get("DATABASE_URL", "").strip()
    or os.environ.get("POSTGRES_URI", "").strip()
)
if raw_db_url:
    if raw_db_url.startswith("postgres://"):
        raw_db_url = raw_db_url.replace("postgres://", "postgresql://", 1)
    DATABASE_URI = raw_db_url
else:
    default_db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "timesheet.db")
    db_path = os.environ.get("SQLITE_DB_PATH", default_db_path).strip() or default_db_path
    DATABASE_URI = f"sqlite:///{db_path}"

# Configure CORS for riverviewfire.org and local development
allowed_origins_env = os.environ.get("CORS_ORIGINS", "") or os.environ.get("ALLOWED_ORIGINS", "")
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


def iso_utc(dt: datetime | None) -> str | None:
    """ISO timestamp with an explicit UTC offset (SQLite drops tzinfo on read)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


class Employee(Base):
    __tablename__ = "employees"

    dept_id = Column(String(3), primary_key=True)  # "900" through "999"
    name = Column(String(120), nullable=False)
    role = Column(String(20), nullable=False, default="member")  # member | officer | cfo
    ot_threshold = Column(Integer, nullable=False, default=40)   # 53 for Full-Time 24/48, 40 for standard staff
    is_driver = Column(Boolean, nullable=False, default=False)  # Qualified apparatus driver (scheduling)
    pin_hash = Column(String(256), nullable=False)
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), default=utc_now)

    entries = relationship("TimeEntry", back_populates="employee", cascade="all, delete-orphan")

    def to_dict(self):
        return {
            "dept_id": self.dept_id,
            "name": self.name,
            "role": self.role,
            "ot_threshold": self.ot_threshold if self.ot_threshold in (40, 53) else 40,
            "is_driver": bool(self.is_driver),
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
    actor_id = Column(String(64), nullable=False)
    actor_name = Column(String(120), nullable=False)
    action = Column(String(60), nullable=False)
    target_type = Column(String(40), nullable=False)
    target_id = Column(String(64), nullable=False)
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


# ------------------------------------------------------------------------------
# Scheduling Models
# ------------------------------------------------------------------------------

class ScheduleSetting(Base):
    """Key/value JSON settings for scheduling (e.g., the staffing template)."""
    __tablename__ = "schedule_settings"

    key = Column(String(60), primary_key=True)
    value = Column(Text, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class ScheduleMonth(Base):
    """One row per scheduled month (YYYY-MM): draft while being built, then published."""
    __tablename__ = "schedule_months"

    id = Column(Integer, primary_key=True, autoincrement=True)
    month = Column(String(7), nullable=False, unique=True, index=True)  # YYYY-MM
    status = Column(String(20), nullable=False, default="draft")       # draft | published
    availability_deadline = Column(String(10), nullable=False)         # YYYY-MM-DD (inclusive)
    notes = Column(Text, nullable=True, default="")
    created_by = Column(String(120), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utc_now)
    published_by = Column(String(120), nullable=True)
    published_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class ScheduleSlot(Base):
    """A single seat on the schedule (date + shift + position + seat number)."""
    __tablename__ = "schedule_slots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    month = Column(String(7), nullable=False, index=True)        # YYYY-MM
    shift_date = Column(String(10), nullable=False, index=True)  # YYYY-MM-DD the shift starts
    shift_key = Column(String(20), nullable=False)               # e.g. day | night
    shift_label = Column(String(40), nullable=False)
    position_key = Column(String(20), nullable=False)            # e.g. st1 | st2 | squad
    position_label = Column(String(40), nullable=False)
    seat = Column(Integer, nullable=False, default=1)
    start_time = Column(String(5), nullable=False)
    end_time = Column(String(5), nullable=False)
    dept_id = Column(String(3), ForeignKey("employees.dept_id"), nullable=True, index=True)
    assigned_via = Column(String(20), nullable=True)             # officer | signup | autofill
    assigned_by = Column(String(120), nullable=True)
    assigned_at = Column(DateTime(timezone=True), nullable=True)
    # Partial coverage: minutes from shift_date 00:00 that the assignee actually works
    # (e.g. 480-1020 = 08:00-17:00). NULL means the full seat. The seat still counts as filled.
    cover_start_min = Column(Integer, nullable=True)
    cover_end_min = Column(Integer, nullable=True)

    employee = relationship("Employee")


class ScheduleAvailability(Base):
    """
    A member's available hours for one operational day (by default 07:00 to
    07:00 the next morning, so a full night shift is one continuous block).
    Bit i of hours_mask = available for the hour starting at (start_hour + i)
    on op_date; bits past midnight fall on the next calendar day.
    """
    __tablename__ = "schedule_availability_hours"
    __table_args__ = (
        UniqueConstraint("dept_id", "op_date", name="uq_schedule_availability_member_day"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    dept_id = Column(String(3), ForeignKey("employees.dept_id"), nullable=False, index=True)
    month = Column(String(7), nullable=False, index=True)
    op_date = Column(String(10), nullable=False, index=True)
    start_hour = Column(Integer, nullable=False, default=7)
    hours_mask = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), default=utc_now)


class ScheduleSubmission(Base):
    """Tracks that a member submitted availability for a month, plus notes / max hours."""
    __tablename__ = "schedule_submissions"
    __table_args__ = (
        UniqueConstraint("dept_id", "month", name="uq_schedule_submission_member_month"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    dept_id = Column(String(3), ForeignKey("employees.dept_id"), nullable=False, index=True)
    month = Column(String(7), nullable=False, index=True)
    notes = Column(Text, nullable=True, default="")
    max_hours = Column(Integer, nullable=True)
    submitted_by = Column(String(120), nullable=True)
    submitted_at = Column(DateTime(timezone=True), default=utc_now)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    def to_dict(self):
        return {
            "dept_id": self.dept_id,
            "month": self.month,
            "notes": self.notes or "",
            "max_hours": self.max_hours,
            "submitted_by": self.submitted_by,
            "submitted_at": iso_utc(self.submitted_at),
            "updated_at": iso_utc(self.updated_at),
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
    if mm % 15 != 0:
        raise ValueError("Start and end times must be in 15-minute intervals (:00, :15, :30, :45).")
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


def get_shift_interval_minutes(shift_date_str: str, start_time_str: str, end_time_str: str) -> tuple[int, int]:
    """
    Returns (start_abs_minutes, end_abs_minutes) from ordinal day 0 so same-day
    and overnight shifts can be compared accurately for overlaps or duplicates.
    """
    dt = parse_shift_date(shift_date_str)
    base_mins = dt.toordinal() * 1440
    s_mins = time_to_minutes(start_time_str)
    e_mins = time_to_minutes(end_time_str)
    start_abs = base_mins + s_mins
    end_abs = (base_mins + 1440 + e_mins) if e_mins <= s_mins else (base_mins + e_mins)
    return start_abs, end_abs


SPLIT_PART1_TAG = "[Part 1/2 - Split at Sun Midnight]"
SPLIT_PART2_TAG = "[Part 2/2 - Split at Sun Midnight]"


def find_split_partner_entry(session, entry: TimeEntry) -> TimeEntry | None:
    """
    If `entry` is one half of an overnight Sunday midnight split shift, finds
    the matching partner entry on the adjacent day for the same employee.
    """
    notes = entry.notes or ""
    try:
        dt = parse_shift_date(entry.shift_date)
    except ValueError:
        return None

    if SPLIT_PART1_TAG in notes and entry.end_time == "24:00":
        mon_iso = (dt + timedelta(days=1)).isoformat()
        candidates = session.query(TimeEntry).filter(
            TimeEntry.dept_id == entry.dept_id,
            TimeEntry.shift_date == mon_iso,
            TimeEntry.start_time == "00:00",
        ).all()
        for c in candidates:
            if SPLIT_PART2_TAG in (c.notes or ""):
                return c
    elif SPLIT_PART2_TAG in notes and entry.start_time == "00:00":
        sun_iso = (dt - timedelta(days=1)).isoformat()
        candidates = session.query(TimeEntry).filter(
            TimeEntry.dept_id == entry.dept_id,
            TimeEntry.shift_date == sun_iso,
            TimeEntry.end_time == "24:00",
        ).all()
        for c in candidates:
            if SPLIT_PART1_TAG in (c.notes or ""):
                return c
    return None


def cleanup_orphaned_split_entries(session, dept_id: str | None = None):
    """
    Removes any pending half of a Sunday midnight split shift whose partner half
    was previously deleted by the user.
    """
    query = session.query(TimeEntry).filter(TimeEntry.status == "pending")
    if dept_id:
        query = query.filter(TimeEntry.dept_id == dept_id)

    removed_any = False
    for entry in query.all():
        notes = entry.notes or ""
        if SPLIT_PART1_TAG in notes or SPLIT_PART2_TAG in notes:
            partner = find_split_partner_entry(session, entry)
            if partner is None:
                session.delete(entry)
                removed_any = True
    if removed_any:
        session.commit()


def check_overlapping_entries(
    session,
    dept_id: str,
    shift_date_str: str,
    start_time_str: str,
    end_time_str: str,
    exclude_entry_id: int | None = None,
) -> str | None:
    """
    Checks if the candidate shift duplicates or overlaps any existing non-rejected
    shift for the same employee. Returns a formatted error message if a conflict
    exists, or None if valid.
    """
    cleanup_orphaned_split_entries(session, dept_id)
    cand_start, cand_end = get_shift_interval_minutes(shift_date_str, start_time_str, end_time_str)
    query = session.query(TimeEntry).filter(
        TimeEntry.dept_id == dept_id,
        TimeEntry.status != "rejected",
    )
    if exclude_entry_id is not None:
        query = query.filter(TimeEntry.id != exclude_entry_id)

    conflicts = []
    has_exact_duplicate = False
    for existing in query.all():
        try:
            ex_start, ex_end = get_shift_interval_minutes(existing.shift_date, existing.start_time, existing.end_time)
        except ValueError:
            continue
        if cand_start < ex_end and ex_start < cand_end:
            if ex_start == cand_start and ex_end == cand_end:
                has_exact_duplicate = True
            overnight_tag = " (overnight +1d)" if existing.is_overnight else ""
            conflicts.append(
                f"{existing.shift_date} from {existing.start_time} to {existing.end_time}{overnight_tag} "
                f"({existing.duration_hours:.2f} hrs, {existing.status}, Pay Period {existing.pay_period_start})"
            )

    if not conflicts:
        return None

    joined = "; ".join(conflicts)
    if has_exact_duplicate and len(conflicts) == 1:
        return f"Duplicate time entry already submitted: {joined}."
    return (
        f"Overlapping time entry already submitted: Your shift on {shift_date_str} "
        f"({start_time_str}–{end_time_str}) overlaps with {joined}."
    )


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
# Database Initialization & Roster CSV Loader
# ==============================================================================

def parse_roster_csv_text(csv_text: str) -> tuple[list[dict], list[str]]:
    """
    Parses a roster CSV (such as Name_and_ID.csv with columns:
    AGENCY PERSONNEL ID, LAST NAME, FIRST NAME, RANK, EMPLOYMENT STATUS).
    Returns (valid_records, skipped_descriptions).
    """
    records = []
    skipped = []
    reader = csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff").strip()))
    for raw_row in reader:
        row = {(k or "").strip().upper(): (v or "").strip() for k, v in raw_row.items()}
        dept_id = (
            row.get("AGENCY PERSONNEL ID")
            or row.get("DEPT_ID")
            or row.get("DEPARTMENT ID")
            or row.get("ID")
            or ""
        ).strip()

        first_name = row.get("FIRST NAME", "")
        last_name = row.get("LAST NAME", "")
        full_name = row.get("NAME") or f"{first_name} {last_name}".strip()

        if not is_valid_dept_id(dept_id):
            label = full_name or "Unknown"
            skipped.append(f"{label} (missing/invalid ID '{dept_id}')")
            continue

        if not full_name:
            skipped.append(f"ID {dept_id} (missing name)")
            continue

        rank = row.get("RANK", "")
        emp_status = row.get("EMPLOYMENT STATUS", "")
        explicit_role = row.get("ROLE", "").lower()

        rank_lower = rank.lower()
        if explicit_role in ("member", "officer", "cfo"):
            role = explicit_role
        elif dept_id == "953" or any(k in rank_lower for k in ("chief", "captain", "lieutenant", "officer")):
            role = "officer"
        elif any(k in rank_lower for k in ("administrator", "support", "cfo", "finance")):
            role = "cfo"
        else:
            role = "member"

        status_lower = emp_status.lower()
        explicit_ot = row.get("OT_THRESHOLD", "")
        if explicit_ot in ("40", "53"):
            ot_threshold = int(explicit_ot)
        elif "full time" in status_lower or "full-time" in status_lower:
            ot_threshold = 53
        else:
            ot_threshold = 40

        # Optional DRIVER / QUALIFIED DRIVER column (yes/no). None = leave unchanged.
        driver_raw = (row.get("DRIVER") or row.get("QUALIFIED DRIVER") or row.get("IS_DRIVER") or "").strip().lower()
        if driver_raw in ("y", "yes", "true", "1", "x"):
            is_driver = True
        elif driver_raw in ("n", "no", "false", "0"):
            is_driver = False
        else:
            is_driver = None

        records.append({
            "dept_id": dept_id,
            "name": full_name,
            "role": role,
            "ot_threshold": ot_threshold,
            "is_driver": is_driver,
            "rank": rank,
            "employment_status": emp_status,
            "pin": row.get("PIN", "").strip(),
        })

    return records, skipped


def upsert_roster_records(session, records: list[dict], default_pin: str = "1234", admin_pin: str | None = None) -> tuple[int, int]:
    """
    Inserts or updates Employee rows from parsed roster records.
    Returns (created_count, updated_count).
    """
    env_admin_pin = os.environ.get("INITIAL_ADMIN_PIN", "").strip()
    effective_admin_pin = env_admin_pin or admin_pin or default_pin
    member_hash = generate_password_hash(default_pin)
    officer_hash = generate_password_hash(effective_admin_pin)
    created = 0
    updated = 0

    for rec in records:
        dept_id = rec["dept_id"]
        is_primary_admin = dept_id == "953"
        role = "officer" if is_primary_admin else rec["role"]
        csv_pin = (rec.get("pin") or "").strip()
        existing = session.query(Employee).filter_by(dept_id=dept_id).first()
        if existing:
            existing.name = rec["name"]
            existing.role = role
            existing.ot_threshold = rec["ot_threshold"]
            if rec.get("is_driver") is not None:
                existing.is_driver = rec["is_driver"]
            if is_primary_admin and env_admin_pin:
                existing.pin_hash = officer_hash
            elif len(csv_pin) >= 4:
                existing.pin_hash = generate_password_hash(csv_pin)
            existing.active = True
            updated += 1
        else:
            if is_primary_admin and env_admin_pin:
                pin_hash = officer_hash
            elif len(csv_pin) >= 4:
                pin_hash = generate_password_hash(csv_pin)
            else:
                pin_hash = officer_hash if role in ("officer", "cfo") else member_hash
            session.add(
                Employee(
                    dept_id=dept_id,
                    name=rec["name"],
                    role=role,
                    ot_threshold=rec["ot_threshold"],
                    is_driver=bool(rec.get("is_driver")),
                    pin_hash=pin_hash,
                    active=True,
                )
            )
            created += 1

    return created, updated


def load_startup_roster_csv() -> str | None:
    """
    Checks for roster CSV content from ROSTER_CSV env var or local Name_and_ID.csv file.
    """
    env_csv = os.environ.get("ROSTER_CSV", "").strip()
    if env_csv:
        return env_csv

    custom_path = os.environ.get("ROSTER_CSV_PATH", "").strip()
    candidate_paths = [
        custom_path,
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "Name_and_ID.csv"),
    ]
    for path in candidate_paths:
        if path and os.path.isfile(path):
            with open(path, "r", encoding="utf-8-sig") as f:
                return f.read()
    return None


def init_db():
    Base.metadata.create_all(bind=engine)
    # Safe column migrations for tables created by older versions. Each runs on its
    # own connection so a failure ("column already exists") can't abort the next one
    # (PostgreSQL aborts the whole transaction after any error).
    for ddl in (
        "ALTER TABLE employees ADD COLUMN ot_threshold INTEGER NOT NULL DEFAULT 40",
        "ALTER TABLE employees ADD COLUMN is_driver BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE schedule_slots ADD COLUMN cover_start_min INTEGER",
        "ALTER TABLE schedule_slots ADD COLUMN cover_end_min INTEGER",
    ):
        with engine.connect() as conn:
            try:
                conn.execute(text(ddl))
                conn.commit()
            except Exception:
                conn.rollback()

    session = SessionLocal()
    try:
        default_admin_pin = os.environ.get("INITIAL_ADMIN_PIN", "1234").strip() or "1234"
        default_member_pin = os.environ.get("INITIAL_MEMBER_PIN", "1234").strip() or "1234"
        admin_hash = generate_password_hash(default_admin_pin)

        # Clean up legacy demo accounts (900-910 from initial prototype or 930 bootstrap) if present
        demo_names = {
            "900": "T. Couch",
            "901": "M. Reynolds",
            "902": "J. Miller",
            "903": "S. Davis",
            "904": "R. Thompson",
            "905": "K. Patel",
            "906": "B. Henderson",
            "907": "A. Morales",
            "908": "D. Brooks",
            "909": "C. Wallace",
            "910": "H. Nguyen",
            "930": "David Hord",
        }
        has_legacy_demo = session.query(Employee).filter_by(dept_id="900", name="T. Couch").first() is not None
        if has_legacy_demo:
            session.query(TimeEntry).filter(TimeEntry.dept_id.in_(list(demo_names.keys()))).delete(synchronize_session=False)
            for d_id, d_name in demo_names.items():
                session.query(Employee).filter_by(dept_id=d_id, name=d_name).delete(synchronize_session=False)
            session.commit()
        else:
            # Also remove 930 bootstrap if it has no shift entries
            emp_930 = session.query(Employee).filter_by(dept_id="930", name="David Hord").first()
            if emp_930 and session.query(TimeEntry).filter_by(dept_id="930").count() == 0:
                session.delete(emp_930)
                session.commit()

        # Always guarantee 953 (Tyler Couch) exists as an active Officer
        admin_953 = session.query(Employee).filter_by(dept_id="953").first()
        if admin_953:
            admin_953.name = "Tyler Couch"
            admin_953.role = "officer"
            has_custom_pin = (
                session.query(AuditLog)
                .filter_by(target_id="953", action="PIN_CHANGED")
                .first()
                is not None
            )
            if not has_custom_pin:
                admin_953.pin_hash = admin_hash
            admin_953.active = True
            session.commit()
        else:
            session.add(
                Employee(
                    dept_id="953",
                    name="Tyler Couch",
                    role="officer",
                    ot_threshold=40,
                    pin_hash=admin_hash,
                    active=True,
                )
            )
            log_audit(
                session,
                actor_id="SYSTEM",
                actor_name="System Initializer",
                action="INITIAL_SEED",
                target_type="employee",
                target_id="953",
                details="Created bootstrap officer account (953 Tyler Couch). Use Roster -> Import CSV to load full roster.",
            )
            session.commit()

        # If a roster CSV is present on disk or in ROSTER_CSV env var and only 953 exists, seed from it
        csv_text = load_startup_roster_csv()
        if session.query(Employee).count() <= 1 and csv_text:
            records, skipped = parse_roster_csv_text(csv_text)
            if records:
                created, updated = upsert_roster_records(
                    session,
                    records,
                    default_pin=default_member_pin,
                    admin_pin=default_admin_pin,
                )
                skip_note = f" Skipped {len(skipped)}: {', '.join(skipped)}." if skipped else ""
                log_audit(
                    session,
                    actor_id="SYSTEM",
                    actor_name="System Initializer",
                    action="INITIAL_ROSTER_SEED",
                    target_type="employee",
                    target_id=f"{created + updated} members",
                    details=f"Seeded department members from Name_and_ID.csv.{skip_note}",
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
# Rate Limiting Helpers (Database-backed across Gunicorn workers)
# ==============================================================================

LOGIN_MAX_ATTEMPTS_PER_ID = int(os.environ.get("LOGIN_MAX_ATTEMPTS_PER_ID", 5))
LOGIN_MAX_ATTEMPTS_PER_IP = int(os.environ.get("LOGIN_MAX_ATTEMPTS_PER_IP", 12))
LOGIN_LOCKOUT_WINDOW_MINUTES = int(os.environ.get("LOGIN_LOCKOUT_WINDOW_MINUTES", 15))

ENTRY_MAX_PER_MINUTE = int(os.environ.get("ENTRY_MAX_PER_MINUTE", 6))
ENTRY_MAX_PER_HOUR = int(os.environ.get("ENTRY_MAX_PER_HOUR", 30))


def get_client_ip() -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        ip = forwarded.split(",")[0].strip()
    else:
        ip = (request.remote_addr or "unknown").strip()
    return ip[:64]


def is_login_rate_limited(session, dept_id: str, client_ip: str) -> str | None:
    """
    Returns an error message if the Department ID or client IP has exceeded
    the allowed failed sign-in attempts within the lockout window.
    """
    window_start = utc_now() - timedelta(minutes=LOGIN_LOCKOUT_WINDOW_MINUTES)

    # If the account logged in successfully or had its PIN updated recently, only count failures since then
    last_reset = (
        session.query(AuditLog.timestamp)
        .filter(
            AuditLog.target_id == dept_id,
            AuditLog.action.in_(["LOGIN_SUCCESS", "PIN_CHANGED", "EMPLOYEE_UPDATED", "ROSTER_CSV_IMPORTED"]),
            AuditLog.timestamp >= window_start,
        )
        .order_by(AuditLog.timestamp.desc())
        .first()
    )
    id_cutoff = last_reset[0] if last_reset else window_start

    failed_for_id = (
        session.query(AuditLog)
        .filter(
            AuditLog.action == "LOGIN_FAILED",
            AuditLog.target_id == dept_id,
            AuditLog.timestamp > id_cutoff,
        )
        .count()
    )
    if failed_for_id >= LOGIN_MAX_ATTEMPTS_PER_ID:
        return (
            f"Too many failed sign-in attempts for ID {dept_id}. "
            f"Please wait {LOGIN_LOCKOUT_WINDOW_MINUTES} minutes or contact Tyler."
        )

    failed_for_ip = (
        session.query(AuditLog)
        .filter(
            AuditLog.action == "LOGIN_FAILED",
            AuditLog.actor_id == client_ip,
            AuditLog.timestamp >= window_start,
        )
        .count()
    )
    if failed_for_ip >= LOGIN_MAX_ATTEMPTS_PER_IP:
        return (
            f"Too many failed sign-in attempts from this network. "
            f"Please wait {LOGIN_LOCKOUT_WINDOW_MINUTES} minutes or contact Tyler."
        )

    return None


def is_entry_rate_limited(session, actor_dept_id: str) -> str | None:
    """
    Returns an error message if the authenticated member is submitting shift
    entries faster than the per-minute or per-hour threshold.
    """
    now = utc_now()
    one_min_ago = now - timedelta(minutes=1)
    one_hour_ago = now - timedelta(hours=1)
    entry_actions = ["ENTRY_CREATED", "ENTRY_CREATED_SPLIT"]

    recent_minute = (
        session.query(AuditLog)
        .filter(
            AuditLog.actor_id == actor_dept_id,
            AuditLog.action.in_(entry_actions),
            AuditLog.timestamp >= one_min_ago,
        )
        .count()
    )
    if recent_minute >= ENTRY_MAX_PER_MINUTE:
        return "Rate limit reached: Too many shifts submitted in the last minute. Please wait a moment and try again."

    recent_hour = (
        session.query(AuditLog)
        .filter(
            AuditLog.actor_id == actor_dept_id,
            AuditLog.action.in_(entry_actions),
            AuditLog.timestamp >= one_hour_ago,
        )
        .count()
    )
    if recent_hour >= ENTRY_MAX_PER_HOUR:
        return f"Rate limit reached: Maximum of {ENTRY_MAX_PER_HOUR} shift submissions per hour. Please wait or contact Tyler."

    return None


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

    client_ip = get_client_ip()
    session = SessionLocal()
    rate_err = is_login_rate_limited(session, dept_id, client_ip)
    if rate_err:
        return jsonify({"error": rate_err}), 429

    emp = session.query(Employee).filter_by(dept_id=dept_id).first()
    if not emp or not check_password_hash(emp.pin_hash, pin):
        log_audit(
            session,
            actor_id=client_ip,
            actor_name="Sign-In Attempt",
            action="LOGIN_FAILED",
            target_type="employee",
            target_id=dept_id,
            details=f"Failed login attempt for ID {dept_id}.",
        )
        session.commit()
        return jsonify({"error": "Invalid Department ID or PIN."}), 401

    if not emp.active:
        return jsonify({"error": "This Department ID is currently inactive. Contact Tyler."}), 403

    log_audit(
        session,
        actor_id=emp.dept_id,
        actor_name=emp.name,
        action="LOGIN_SUCCESS",
        target_type="employee",
        target_id=emp.dept_id,
        details="Signed in.",
    )
    session.commit()

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
    client_ip = get_client_ip()
    rate_err = is_login_rate_limited(g.db, emp.dept_id, client_ip)
    if rate_err:
        return jsonify({"error": rate_err}), 429

    if not check_password_hash(emp.pin_hash, current_pin):
        log_audit(
            g.db,
            actor_id=client_ip,
            actor_name=emp.name,
            action="LOGIN_FAILED",
            target_type="employee",
            target_id=emp.dept_id,
            details="Failed current PIN verification during PIN change.",
        )
        g.db.commit()
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

    cleanup_orphaned_split_entries(g.db, None if is_reviewer else emp.dept_id)
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

    rate_err = is_entry_rate_limited(g.db, emp.dept_id)
    if rate_err:
        return jsonify({"error": rate_err}), 429

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

    if shift_dt > date.today() + timedelta(days=1):
        return jsonify({"error": "Shift date cannot be more than 1 day in the future."}), 400

    conflict_err = check_overlapping_entries(
        g.db,
        dept_id=target_dept_id,
        shift_date_str=shift_dt.isoformat(),
        start_time_str=start_time_str,
        end_time_str=end_time_str,
        exclude_entry_id=None,
    )
    if conflict_err:
        return jsonify({"error": conflict_err}), 409

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

    if shift_dt > date.today() + timedelta(days=1):
        return jsonify({"error": "Shift date cannot be more than 1 day in the future."}), 400

    conflict_err = check_overlapping_entries(
        g.db,
        dept_id=entry.dept_id,
        shift_date_str=shift_dt.isoformat(),
        start_time_str=start_time_str,
        end_time_str=end_time_str,
        exclude_entry_id=entry.id,
    )
    if conflict_err:
        return jsonify({"error": conflict_err}), 409

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

    partner = find_split_partner_entry(g.db, entry)
    deleted_ids = [str(entry.id)]
    details = f"Deleted entry {entry.id} for {entry.dept_id} ({entry.shift_date} {entry.start_time}-{entry.end_time}, {entry.duration_hours}h)"
    g.db.delete(entry)

    if partner and (is_reviewer or partner.status == "pending"):
        deleted_ids.append(str(partner.id))
        details += f" and split partner entry {partner.id} ({partner.shift_date} {partner.start_time}-{partner.end_time}, {partner.duration_hours}h)"
        g.db.delete(partner)

    log_audit(
        g.db,
        actor_id=emp.dept_id,
        actor_name=emp.name,
        action="ENTRY_DELETED",
        target_type="time_entry",
        target_id=",".join(deleted_ids),
        details=details,
    )
    g.db.commit()
    return jsonify({"message": "Time entry deleted.", "deleted_ids": [int(i) for i in deleted_ids]})


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

    if new_status in ("approved", "pending"):
        conflict_err = check_overlapping_entries(
            g.db,
            dept_id=entry.dept_id,
            shift_date_str=entry.shift_date,
            start_time_str=entry.start_time,
            end_time_str=entry.end_time,
            exclude_entry_id=entry.id,
        )
        if conflict_err:
            return jsonify({"error": conflict_err}), 409

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
            ot_thresh = emp_obj.ot_threshold if (emp_obj and emp_obj.ot_threshold in (40, 53)) else 40
            by_employee[e.dept_id] = {
                "dept_id": e.dept_id,
                "name": emp_obj.name if emp_obj else f"ID {e.dept_id}",
                "role": emp_obj.role if emp_obj else "member",
                "ot_threshold": ot_thresh,
                "regular_hours": 0.0,
                "overtime_hours": 0.0,
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
    for r in summary_rows:
        thresh = r["ot_threshold"]
        r["regular_hours"] = round(min(r["approved_hours"], thresh), 2)
        r["overtime_hours"] = round(max(0.0, r["approved_hours"] - thresh), 2)

    totals = {
        "employee_count": len(summary_rows),
        "total_shifts": sum(r["shift_count"] for r in summary_rows),
        "approved_shifts": sum(r["approved_shift_count"] for r in summary_rows),
        "regular_hours": round(sum(r["regular_hours"] for r in summary_rows), 2),
        "overtime_hours": round(sum(r["overtime_hours"] for r in summary_rows), 2),
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
            "OT Threshold (Hrs)",
            "Regular Approved Hours",
            "Overtime Approved Hours",
            "Total Approved Hours (Payroll)",
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
                row["ot_threshold"],
                f"{row['regular_hours']:.2f}",
                f"{row['overtime_hours']:.2f}",
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
            "",
            f"{t['regular_hours']:.2f}",
            f"{t['overtime_hours']:.2f}",
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
    ot_threshold = 53 if int(data.get("ot_threshold", 40) or 40) == 53 else 40
    is_driver = bool(data.get("is_driver", False))
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
        ot_threshold=ot_threshold,
        is_driver=is_driver,
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
        details=(
            f"Added {dept_id} ({name}) with role '{role}' and {ot_threshold}h OT threshold"
            + (", qualified driver." if is_driver else ".")
        ),
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

    if "ot_threshold" in data:
        new_ot = 53 if int(data["ot_threshold"] or 40) == 53 else 40
        if new_ot != emp.ot_threshold:
            changes.append(f"ot_threshold {emp.ot_threshold}h -> {new_ot}h")
            emp.ot_threshold = new_ot

    if "is_driver" in data:
        new_driver = bool(data["is_driver"])
        if new_driver != bool(emp.is_driver):
            changes.append(f"qualified driver {bool(emp.is_driver)} -> {new_driver}")
            emp.is_driver = new_driver

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


@app.route("/api/employees/import-csv", methods=["POST"])
@require_reviewer
def import_employees_csv():
    actor = g.current_user
    data = request.get_json(silent=True) or {}
    csv_text = str(data.get("csv_text", "")).strip()
    default_pin = str(data.get("default_pin") or os.environ.get("INITIAL_MEMBER_PIN", "1234")).strip()

    if not csv_text:
        return jsonify({"error": "CSV content is required."}), 400
    if len(default_pin) < 4:
        return jsonify({"error": "Default PIN must be at least 4 characters."}), 400

    records, skipped = parse_roster_csv_text(csv_text)
    if not records:
        return jsonify({"error": "No valid 900–999 employee rows found in CSV.", "skipped": skipped}), 400

    created, updated = upsert_roster_records(g.db, records, default_pin=default_pin)
    skip_msg = f" ({len(skipped)} skipped)" if skipped else ""
    log_audit(
        g.db,
        actor_id=actor.dept_id,
        actor_name=actor.name,
        action="ROSTER_CSV_IMPORTED",
        target_type="employee",
        target_id=f"{len(records)} members",
        details=f"Imported roster CSV: {created} added, {updated} updated{skip_msg}.",
    )
    g.db.commit()

    return jsonify({
        "message": f"Imported {len(records)} members ({created} added, {updated} updated){skip_msg}.",
        "created": created,
        "updated": updated,
        "skipped": skipped,
    })


@app.route("/api/audit-logs", methods=["GET"])
@require_reviewer
def get_audit_logs():
    limit = min(int(request.args.get("limit", 100)), 500)
    logs = g.db.query(AuditLog).order_by(AuditLog.timestamp.desc(), AuditLog.id.desc()).limit(limit).all()
    return jsonify({"logs": [l.to_dict() for l in logs]})


# ==============================================================================
# Scheduling: Staffing Template, Availability, Builder & Vacancy Sign-Ups
# ==============================================================================
#
# Workflow:
#   1. Members drag across an hour-by-hour grid (1-hour granularity) to mark
#      when they can work in an upcoming month. It's due `availability_lead_days`
#      (default 14) before the month starts; officers can move the deadline.
#   2. An officer fills seats in the Builder, by hand or with Auto-Fill, while
#      watching each member's projected hours per Monday-Sunday pay week against
#      their OT threshold (40h standard / 53h 24-48). Someone available for only
#      part of a shift (e.g. can't start until 08:00) can still fill the seat: the
#      seat counts as filled and their hours reflect the part they work.
#      Positions marked `requires_driver` need a qualified driver on every shift.
#   3. The officer publishes the month. Members can then sign up for open seats.

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo(os.environ.get("LOCAL_TIMEZONE", "America/New_York"))
except Exception:  # tz database unavailable (e.g. Windows without the tzdata package)
    LOCAL_TZ = timezone(timedelta(hours=-5))

SIGNUP_DROP_CUTOFF_HOURS = int(os.environ.get("SIGNUP_DROP_CUTOFF_HOURS", 24))
MAX_CONTINUOUS_HOURS = int(os.environ.get("SCHEDULE_MAX_CONTINUOUS_HOURS", 24))  # Auto-Fill rest rule
MONTH_REGEX = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
TEMPLATE_KEY_REGEX = re.compile(r"^[a-z0-9_]{1,20}$")
FULL_DAY_MASK = (1 << 24) - 1

DEFAULT_STAFFING_TEMPLATE = {
    "positions": [
        {"key": "st1", "label": "Station 1", "requires_driver": True},
        {"key": "st2", "label": "Station 2", "requires_driver": True},
        {"key": "squad", "label": "Squad", "requires_driver": False},
    ],
    "shifts": [
        {
            "key": "day",
            "label": "Day",
            "start": "07:00",
            "end": "17:00",
            "staffing": {"st1": 2, "st2": 2, "squad": 1},
        },
        {
            "key": "night",
            "label": "Night",
            "start": "17:00",
            "end": "07:00",
            "staffing": {"st1": 1, "st2": 1, "squad": 0},
        },
    ],
    "availability_lead_days": 14,
    # Partial availability counts toward a shift when it covers at least this share of it
    "min_coverage_pct": 75,
}


def local_now() -> datetime:
    return datetime.now(LOCAL_TZ)


def local_today() -> date:
    return local_now().date()


def local_abs_minutes(dt: datetime) -> int:
    """Minutes on the same absolute timeline as get_shift_interval_minutes()."""
    return dt.date().toordinal() * 1440 + dt.hour * 60 + dt.minute


def is_reviewer_role(emp: Employee) -> bool:
    return emp.role in ("officer", "cfo")


def actor_label(emp: Employee) -> str:
    return f"{emp.dept_id} {emp.name}"


def parse_month(month_str: str) -> date:
    month_str = (month_str or "").strip()
    if not MONTH_REGEX.match(month_str):
        raise ValueError("Month must be in YYYY-MM format.")
    year, month = map(int, month_str.split("-"))
    return date(year, month, 1)


def add_months(first: date, delta: int) -> date:
    idx = first.year * 12 + (first.month - 1) + delta
    return date(idx // 12, idx % 12 + 1, 1)


def month_last_day(first: date) -> date:
    return add_months(first, 1) - timedelta(days=1)


def month_offset(first: date) -> int:
    today = local_today()
    return (first.year - today.year) * 12 + (first.month - today.month)


def iter_month_dates(first: date):
    d = first
    last = month_last_day(first)
    while d <= last:
        yield d
        d += timedelta(days=1)


def month_display(month_str: str) -> str:
    return parse_month(month_str).strftime("%B %Y")


def default_availability_deadline(first: date, lead_days: int) -> str:
    return (first - timedelta(days=lead_days)).isoformat()


def minutes_to_hhmm(mins: int) -> str:
    return f"{(mins // 60) % 24:02d}:{mins % 60:02d}"


def validate_schedule_month(month_str: str, emp: Employee) -> tuple[date | None, str | None]:
    try:
        first = parse_month(month_str)
    except ValueError as err:
        return None, str(err)
    low, high = (-24, 18) if is_reviewer_role(emp) else (-12, 6)
    if not low <= month_offset(first) <= high:
        return None, "That month is outside the scheduling window."
    return first, None


def validate_staffing_template(data) -> tuple[dict | None, str | None]:
    """Validates and normalizes a staffing template. Returns (template, error)."""
    if not isinstance(data, dict):
        return None, "Staffing template must be an object."
    positions_in = data.get("positions")
    shifts_in = data.get("shifts")
    if not isinstance(positions_in, list) or not 1 <= len(positions_in) <= 8:
        return None, "Staffing template needs between 1 and 8 positions."
    if not isinstance(shifts_in, list) or not 1 <= len(shifts_in) <= 4:
        return None, "Staffing template needs between 1 and 4 shifts."

    positions = []
    for p in positions_in:
        p = p if isinstance(p, dict) else {}
        key = str(p.get("key", "")).strip().lower()
        label = str(p.get("label", "")).strip()[:40]
        if not TEMPLATE_KEY_REGEX.match(key) or any(x["key"] == key for x in positions):
            return None, f"Invalid or duplicate position key '{key}'."
        if not label:
            return None, f"Position '{key}' needs a label."
        positions.append({"key": key, "label": label, "requires_driver": bool(p.get("requires_driver", False))})

    shifts = []
    for s in shifts_in:
        s = s if isinstance(s, dict) else {}
        key = str(s.get("key", "")).strip().lower()
        label = str(s.get("label", "")).strip()[:40]
        if not TEMPLATE_KEY_REGEX.match(key) or any(x["key"] == key for x in shifts):
            return None, f"Invalid or duplicate shift key '{key}'."
        if not label:
            return None, f"Shift '{key}' needs a label."
        start = str(s.get("start", "")).strip()
        end = str(s.get("end", "")).strip()
        try:
            calculate_duration(start, end)
        except ValueError as err:
            return None, f"{label} shift: {err}"
        staffing_in = s.get("staffing") if isinstance(s.get("staffing"), dict) else {}
        staffing = {}
        for p in positions:
            try:
                count = int(staffing_in.get(p["key"], 0) or 0)
            except (TypeError, ValueError):
                return None, f"Staffing for {label} / {p['label']} must be a whole number."
            if not 0 <= count <= 6:
                return None, f"Staffing for {label} / {p['label']} must be between 0 and 6."
            staffing[p["key"]] = count
        shifts.append({"key": key, "label": label, "start": start, "end": end, "staffing": staffing})

    try:
        lead_days = int(data.get("availability_lead_days", 14))
    except (TypeError, ValueError):
        return None, "Availability lead time must be a whole number of days."
    if not 0 <= lead_days <= 60:
        return None, "Availability lead time must be between 0 and 60 days."

    try:
        min_coverage = int(data.get("min_coverage_pct", 75))
    except (TypeError, ValueError):
        return None, "Minimum coverage must be a whole-number percent."
    if not 10 <= min_coverage <= 100:
        return None, "Minimum coverage must be between 10% and 100%."

    return {
        "positions": positions,
        "shifts": shifts,
        "availability_lead_days": lead_days,
        "min_coverage_pct": min_coverage,
    }, None


def get_staffing_template(session) -> dict:
    row = session.get(ScheduleSetting, "staffing_template")
    if row:
        try:
            tpl, _ = validate_staffing_template(json.loads(row.value))
            if tpl:
                return tpl
        except (TypeError, ValueError):
            pass
    return copy.deepcopy(DEFAULT_STAFFING_TEMPLATE)


def template_day_start_hour(tpl: dict) -> int:
    """The availability grid's day runs from the first shift's start hour to the same hour next day."""
    return time_to_minutes(tpl["shifts"][0]["start"]) // 60


def driver_positions(tpl: dict) -> set:
    return {p["key"] for p in tpl["positions"] if p.get("requires_driver")}


# ------------------------------------------------------------------------------
# Seat time math
# ------------------------------------------------------------------------------

def slot_seat_interval(slot: ScheduleSlot) -> tuple[int, int]:
    return get_shift_interval_minutes(slot.shift_date, slot.start_time, slot.end_time)


def slot_work_interval(slot: ScheduleSlot) -> tuple[int, int]:
    """The minutes the assignee actually works (partial coverage), else the whole seat."""
    if slot.cover_start_min is not None and slot.cover_end_min is not None:
        base = parse_shift_date(slot.shift_date).toordinal() * 1440
        return base + slot.cover_start_min, base + slot.cover_end_min
    return slot_seat_interval(slot)


def slot_hours(slot: ScheduleSlot) -> float:
    try:
        start, end = slot_work_interval(slot)
    except ValueError:
        return 0.0
    return round((end - start) / 60.0, 2)


def slot_work_times(slot: ScheduleSlot) -> tuple[str, str, str]:
    """(work_date, start HH:MM, end HH:MM) for timesheet logging."""
    start, end = slot_work_interval(slot)
    day = start // 1440
    start_rel, end_rel = start - day * 1440, end - day * 1440
    end_str = "24:00" if end_rel == 1440 else minutes_to_hhmm(end_rel)
    return date.fromordinal(day).isoformat(), minutes_to_hhmm(start_rel), end_str


def slot_start_local(slot: ScheduleSlot) -> datetime:
    mins = time_to_minutes(slot.start_time)
    return datetime.combine(parse_shift_date(slot.shift_date), dt_time(mins // 60, mins % 60), tzinfo=LOCAL_TZ)


def describe_slot(slot: ScheduleSlot) -> str:
    try:
        _, start, end = slot_work_times(slot)
    except ValueError:
        start, end = slot.start_time, slot.end_time
    return f"{slot.shift_date} {slot.shift_label} {start}-{end} ({slot.position_label})"


def group_key(slot: ScheduleSlot) -> tuple[str, str, str]:
    return slot.shift_date, slot.shift_key, slot.position_key


def interval_week_segments(start_abs: int, end_abs: int) -> list[tuple[str, float]]:
    """
    Splits an absolute interval into Monday-Sunday pay weeks, mirroring the
    timesheet (an overnight Sunday shift puts the hours after midnight in the
    next week). Returns [(monday_iso, hours), ...].
    """
    segments = []
    t = start_abs
    while t < end_abs:
        d = date.fromordinal(t // 1440)
        monday = d - timedelta(days=d.weekday())
        seg_end = min(end_abs, (monday.toordinal() + 7) * 1440)
        segments.append((monday.isoformat(), (seg_end - t) / 60.0))
        t = seg_end
    return segments


def slot_to_dict(slot: ScheduleSlot) -> dict:
    try:
        is_overnight = time_to_minutes(slot.end_time) <= time_to_minutes(slot.start_time)
        seat_start, seat_end = slot_seat_interval(slot)
        work_date, work_start, work_end = slot_work_times(slot)
    except ValueError:
        is_overnight, seat_start, seat_end = False, 0, 0
        work_date, work_start, work_end = slot.shift_date, slot.start_time, slot.end_time
    return {
        "id": slot.id,
        "month": slot.month,
        "shift_date": slot.shift_date,
        "shift_key": slot.shift_key,
        "shift_label": slot.shift_label,
        "position_key": slot.position_key,
        "position_label": slot.position_label,
        "seat": slot.seat,
        "start_time": slot.start_time,
        "end_time": slot.end_time,
        "seat_hours": round((seat_end - seat_start) / 60.0, 2),
        "hours": slot_hours(slot),
        "is_overnight": is_overnight,
        "cover_start_min": slot.cover_start_min,
        "cover_end_min": slot.cover_end_min,
        "is_partial": slot.cover_start_min is not None,
        "work_date": work_date,
        "work_start": work_start,
        "work_end": work_end,
        "dept_id": slot.dept_id,
        "employee_name": slot.employee.name if slot.employee else None,
        "is_driver": bool(slot.employee and slot.employee.is_driver),
        "assigned_via": slot.assigned_via,
        "assigned_by": slot.assigned_by,
        "assigned_at": iso_utc(slot.assigned_at),
    }


def slot_sort_key_factory(tpl: dict):
    shift_order = {s["key"]: i for i, s in enumerate(tpl["shifts"])}
    pos_order = {p["key"]: i for i, p in enumerate(tpl["positions"])}

    def sort_key(s: ScheduleSlot):
        return (
            s.shift_date,
            shift_order.get(s.shift_key, 99),
            s.shift_key,
            pos_order.get(s.position_key, 99),
            s.position_key,
            s.seat,
            s.id or 0,
        )

    return sort_key


def load_month_slots(session, month_str: str, tpl: dict) -> list:
    # Warm the identity map so slot.employee doesn't issue one query per seat
    session.query(Employee).all()
    return sorted(session.query(ScheduleSlot).filter_by(month=month_str).all(), key=slot_sort_key_factory(tpl))


# ------------------------------------------------------------------------------
# Availability (hourly)
# ------------------------------------------------------------------------------

def availability_abs_hours(rows) -> set:
    """Absolute hour indices (ordinal * 24 + hour) a member marked available."""
    hours = set()
    for r in rows:
        base = parse_shift_date(r.op_date).toordinal() * 24 + (r.start_hour or 0)
        mask = r.hours_mask or 0
        i = 0
        while mask:
            if mask & 1:
                hours.add(base + i)
            mask >>= 1
            i += 1
    return hours


def load_availability_hours(session, first: date, last: date, dept_ids: list | None = None) -> dict:
    """{dept_id: set(absolute hours)} for operational days touching first..last."""
    query = session.query(ScheduleAvailability).filter(
        ScheduleAvailability.op_date >= (first - timedelta(days=1)).isoformat(),
        ScheduleAvailability.op_date <= (last + timedelta(days=1)).isoformat(),
    )
    if dept_ids is not None:
        query = query.filter(ScheduleAvailability.dept_id.in_(dept_ids or [""]))
    by_dept = defaultdict(list)
    for row in query.all():
        by_dept[row.dept_id].append(row)
    return {dept: availability_abs_hours(rows) for dept, rows in by_dept.items()}


def masks_for_month(abs_hours: set, first: date, start_hour: int) -> dict:
    """{op_date: 24-bit mask} for each operational day of the month, relative to start_hour."""
    days = {}
    if not abs_hours:
        return days
    for d in iter_month_dates(first):
        base = d.toordinal() * 24 + start_hour
        mask = 0
        for i in range(24):
            if base + i in abs_hours:
                mask |= 1 << i
        if mask:
            days[d.isoformat()] = mask
    return days


def seat_coverage(avail_hours: set, start_abs: int, end_abs: int) -> tuple[float, int, int]:
    """
    Largest contiguous run of available hours inside a seat [start_abs, end_abs)
    (absolute minutes). Returns (share_of_seat 0-1, run_start_abs, run_end_abs).
    """
    total = end_abs - start_abs
    if total <= 0 or not avail_hours:
        return 0.0, start_abs, start_abs
    best_len, best_start, best_end = 0, start_abs, start_abs
    run_start = None
    first_hour = start_abs // 60
    last_hour = (end_abs - 1) // 60
    for hour in range(first_hour, last_hour + 2):  # one past the end closes the final run
        available = hour <= last_hour and hour in avail_hours
        if available and run_start is None:
            run_start = hour
        elif not available and run_start is not None:
            s = max(start_abs, run_start * 60)
            e = min(end_abs, hour * 60)
            if e - s > best_len:
                best_len, best_start, best_end = e - s, s, e
            run_start = None
    return best_len / total, best_start, best_end


def cover_for_member(avail_hours: set, slot: ScheduleSlot) -> tuple[int | None, int | None]:
    """Partial-coverage minutes for an assignee, or (None, None) to work the full seat."""
    try:
        seat_start, seat_end = slot_seat_interval(slot)
    except ValueError:
        return None, None
    share, run_start, run_end = seat_coverage(avail_hours, seat_start, seat_end)
    if 0 < share < 0.999:
        base = parse_shift_date(slot.shift_date).toordinal() * 1440
        return run_start - base, run_end - base
    return None, None


# ------------------------------------------------------------------------------
# Months, seats, hours & coverage
# ------------------------------------------------------------------------------

def schedule_month_to_dict(m: ScheduleMonth | None, month_str: str, tpl: dict) -> dict:
    first = parse_month(month_str)
    today = local_today()
    if m is None:
        deadline = default_availability_deadline(first, tpl["availability_lead_days"])
        status = "draft"
        extra = {"exists": False, "notes": "", "published_at": None, "published_by": None, "created_at": None}
    else:
        deadline = m.availability_deadline
        status = m.status
        extra = {
            "exists": True,
            "notes": m.notes or "",
            "published_at": iso_utc(m.published_at),
            "published_by": m.published_by,
            "created_at": iso_utc(m.created_at),
        }
    return {
        "month": month_str,
        "label": first.strftime("%B %Y"),
        "first_day": first.isoformat(),
        "last_day": month_last_day(first).isoformat(),
        "status": status,
        "availability_deadline": deadline,
        "availability_open": status != "published" and today.isoformat() <= deadline,
        "days_until_deadline": (parse_shift_date(deadline) - today).days,
        **extra,
    }


def sync_month_slots(session, month_obj: ScheduleMonth, tpl: dict) -> dict:
    """
    Reconciles a month's seats with the staffing template: adds missing seats,
    removes extra *open* seats (including ones added by hand), and refreshes
    labels/times. Filled seats are never removed.
    """
    first = parse_month(month_obj.month)
    by_key = defaultdict(list)
    for s in session.query(ScheduleSlot).filter_by(month=month_obj.month).all():
        by_key[group_key(s)].append(s)

    stats = {"added": 0, "removed": 0, "updated": 0, "kept_filled": 0}
    wanted = set()
    for d in iter_month_dates(first):
        iso = d.isoformat()
        for sh in tpl["shifts"]:
            for p in tpl["positions"]:
                key = (iso, sh["key"], p["key"])
                wanted.add(key)
                count = int(sh["staffing"].get(p["key"], 0))
                seats = sorted(by_key.get(key, []), key=lambda s: s.seat)

                meta = (sh["label"], p["label"], sh["start"], sh["end"])
                for s in seats:
                    if (s.shift_label, s.position_label, s.start_time, s.end_time) != meta:
                        if (s.start_time, s.end_time) != (sh["start"], sh["end"]):
                            s.cover_start_min = None  # times changed: partial window no longer applies
                            s.cover_end_min = None
                        s.shift_label, s.position_label, s.start_time, s.end_time = meta
                        stats["updated"] += 1

                if len(seats) < count:
                    next_seat = max((s.seat for s in seats), default=0) + 1
                    for i in range(count - len(seats)):
                        session.add(ScheduleSlot(
                            month=month_obj.month,
                            shift_date=iso,
                            shift_key=sh["key"],
                            shift_label=sh["label"],
                            position_key=p["key"],
                            position_label=p["label"],
                            seat=next_seat + i,
                            start_time=sh["start"],
                            end_time=sh["end"],
                        ))
                        stats["added"] += 1
                elif len(seats) > count:
                    extra = len(seats) - count
                    for s in sorted(seats, key=lambda s: -s.seat):
                        if extra == 0:
                            break
                        if s.dept_id is None:
                            session.delete(s)
                            stats["removed"] += 1
                            extra -= 1
                    stats["kept_filled"] += extra

    # Seats for shifts/positions that no longer exist in the template
    for key, seats in by_key.items():
        if key in wanted:
            continue
        for s in seats:
            if s.dept_id is None:
                session.delete(s)
                stats["removed"] += 1
            else:
                stats["kept_filled"] += 1

    session.flush()
    return stats


def get_or_create_schedule_month(session, month_str: str, created_by: str) -> ScheduleMonth:
    m = session.query(ScheduleMonth).filter_by(month=month_str).first()
    if m:
        return m
    tpl = get_staffing_template(session)
    first = parse_month(month_str)
    m = ScheduleMonth(
        month=month_str,
        status="draft",
        availability_deadline=default_availability_deadline(first, tpl["availability_lead_days"]),
        notes="",
        created_by=created_by,
    )
    session.add(m)
    try:
        session.flush()
    except IntegrityError:
        # Another request created the same month at the same moment
        session.rollback()
        return session.query(ScheduleMonth).filter_by(month=month_str).first()
    sync_month_slots(session, m, tpl)
    return m


def find_schedule_conflict(session, dept_id: str, slot: ScheduleSlot, start_abs: int | None = None,
                           end_abs: int | None = None) -> ScheduleSlot | None:
    """Returns another seat held by `dept_id` whose working hours overlap, if any."""
    try:
        if start_abs is None or end_abs is None:
            start_abs, end_abs = slot_work_interval(slot)
    except ValueError:
        return None
    d = parse_shift_date(slot.shift_date)
    nearby = session.query(ScheduleSlot).filter(
        ScheduleSlot.dept_id == dept_id,
        ScheduleSlot.id != slot.id,
        ScheduleSlot.shift_date >= (d - timedelta(days=1)).isoformat(),
        ScheduleSlot.shift_date <= (d + timedelta(days=1)).isoformat(),
    ).all()
    for other in nearby:
        try:
            o_start, o_end = slot_work_interval(other)
        except ValueError:
            continue
        if start_abs < o_end and o_start < end_abs:
            return other
    return None


def continuous_block_minutes(intervals: list, start_abs: int, end_abs: int) -> int:
    """Length of the back-to-back block a new interval would join (touching counts)."""
    block_start, block_end = start_abs, end_abs
    changed = True
    while changed:
        changed = False
        for s, e in intervals:
            if s <= block_end and e >= block_start and (s < block_start or e > block_end):
                block_start, block_end = min(block_start, s), max(block_end, e)
                changed = True
    return block_end - block_start


def compute_schedule_hours(session, month_str: str, employees: list, tpl: dict,
                           published_only: bool = False, avail: dict | None = None) -> dict:
    """
    Projects scheduled hours for each employee: month total plus every
    Monday-Sunday pay week that touches the month. Week totals include seats
    from neighboring months so overtime at month boundaries is accurate.
    Also reports how much availability each member gave.
    """
    first = parse_month(month_str)
    last = month_last_day(first)
    first_monday = first - timedelta(days=first.weekday())
    last_monday = last - timedelta(days=last.weekday())

    weeks = []
    wk = first_monday
    while wk <= last_monday:
        weeks.append({"start": wk.isoformat(), "end": (wk + timedelta(days=6)).isoformat()})
        wk += timedelta(days=7)
    week_starts = {w["start"] for w in weeks}

    dept_ids = [e.dept_id for e in employees]
    query = session.query(ScheduleSlot).filter(
        ScheduleSlot.dept_id.isnot(None),
        # Include the Sunday before the first week (overnight spill into Monday)
        ScheduleSlot.shift_date >= (first_monday - timedelta(days=1)).isoformat(),
        ScheduleSlot.shift_date <= (last_monday + timedelta(days=6)).isoformat(),
    )
    if len(dept_ids) <= 5:
        query = query.filter(ScheduleSlot.dept_id.in_(dept_ids or [""]))
    slots = query.all()

    if published_only:
        months = sorted({s.month for s in slots})
        published = {
            row.month for row in session.query(ScheduleMonth.month).filter(
                ScheduleMonth.month.in_(months), ScheduleMonth.status == "published"
            ).all()
        } if months else set()
        slots = [s for s in slots if s.month in published]

    if avail is None:
        avail = load_availability_hours(session, first, last, dept_ids if len(dept_ids) <= 5 else None)
    submissions = {s.dept_id: s for s in session.query(ScheduleSubmission).filter_by(month=month_str).all()}
    threshold = tpl["min_coverage_pct"] / 100.0
    start_hour = template_day_start_hour(tpl)
    window_lo = first.toordinal() * 24 + start_hour
    window_hi = (last.toordinal() + 1) * 24 + start_hour
    template_seats = []  # (start_abs, end_abs) for every template shift of the month
    for d in iter_month_dates(first):
        for sh in tpl["shifts"]:
            template_seats.append(get_shift_interval_minutes(d.isoformat(), sh["start"], sh["end"]))

    rows = {}
    for e in employees:
        rows[e.dept_id] = {
            "dept_id": e.dept_id,
            "name": e.name,
            "role": e.role,
            "is_driver": bool(e.is_driver),
            "ot_threshold": e.ot_threshold if e.ot_threshold in (40, 53) else 40,
            "month_hours": 0.0,
            "month_shifts": 0,
            "partial_shifts": 0,
            "shifts_by_type": {},
            "by_week": {w["start"]: 0.0 for w in weeks},
        }

    first_iso, last_iso = first.isoformat(), last.isoformat()
    for s in slots:
        r = rows.get(s.dept_id)
        if not r:
            continue
        try:
            work_start, work_end = slot_work_interval(s)
        except ValueError:
            continue
        for wk_start, hrs in interval_week_segments(work_start, work_end):
            if wk_start in week_starts:
                r["by_week"][wk_start] += hrs
        if first_iso <= s.shift_date <= last_iso:
            r["month_hours"] += (work_end - work_start) / 60.0
            r["month_shifts"] += 1
            if s.cover_start_min is not None:
                r["partial_shifts"] += 1
            r["shifts_by_type"][s.shift_key] = r["shifts_by_type"].get(s.shift_key, 0) + 1

    for dept_id, r in rows.items():
        thr = r["ot_threshold"]
        r["by_week"] = {k: round(v, 2) for k, v in r["by_week"].items()}
        r["month_hours"] = round(r["month_hours"], 2)
        r["ot_weeks"] = [k for k, v in r["by_week"].items() if v > thr]
        r["projected_ot_hours"] = round(sum(max(0.0, v - thr) for v in r["by_week"].values()), 2)
        hours = avail.get(dept_id, set())
        r["available_hours"] = sum(1 for h in hours if window_lo <= h < window_hi)
        r["available_shifts"] = sum(
            1 for s_abs, e_abs in template_seats if hours and seat_coverage(hours, s_abs, e_abs)[0] + 1e-9 >= threshold
        ) if hours else 0
        sub = submissions.get(dept_id)
        r["submitted"] = sub is not None
        r["max_hours"] = sub.max_hours if sub else None
        r["availability_notes"] = (sub.notes or "") if sub else ""
        r["over_max"] = bool(sub and sub.max_hours is not None and r["month_hours"] > sub.max_hours)

    members = sorted(rows.values(), key=lambda r: r["dept_id"])
    totals = {
        "scheduled_hours": round(sum(r["month_hours"] for r in members), 2),
        "projected_ot_hours": round(sum(r["projected_ot_hours"] for r in members), 2),
        "members_scheduled": sum(1 for r in members if r["month_shifts"] > 0),
        "members_with_ot": sum(1 for r in members if r["ot_weeks"]),
        "members_submitted": sum(1 for r in members if r["submitted"]),
        "member_count": len(members),
    }
    return {"weeks": weeks, "members": members, "totals": totals}


def summarize_coverage(slots: list, tpl: dict) -> dict:
    by_shift = {sh["key"]: {"label": sh["label"], "total": 0, "filled": 0} for sh in tpl["shifts"]}
    filled = 0
    required = driver_positions(tpl)
    crews = defaultdict(list)
    for s in slots:
        bucket = by_shift.setdefault(s.shift_key, {"label": s.shift_label, "total": 0, "filled": 0})
        bucket["total"] += 1
        if s.dept_id:
            bucket["filled"] += 1
            filled += 1
        if s.position_key in required:
            crews[group_key(s)].append(s)

    with_driver = sum(1 for seats in crews.values() if any(s.dept_id and s.employee and s.employee.is_driver for s in seats))
    staffed_no_driver = sum(
        1 for seats in crews.values()
        if any(s.dept_id for s in seats) and not any(s.dept_id and s.employee and s.employee.is_driver for s in seats)
    )
    return {
        "total": len(slots),
        "filled": filled,
        "open": len(slots) - filled,
        "by_shift": by_shift,
        "drivers": {
            "required": len(crews),
            "covered": with_driver,
            "missing": len(crews) - with_driver,
            "staffed_without_driver": staffed_no_driver,
        },
    }


def seat_reserved_for_driver(session, slot: ScheduleSlot, tpl: dict) -> bool:
    """True when this is the last open seat on a driver-required crew that has no driver yet."""
    if slot.position_key not in driver_positions(tpl) or slot.dept_id:
        return False
    crew = session.query(ScheduleSlot).filter_by(
        shift_date=slot.shift_date, shift_key=slot.shift_key, position_key=slot.position_key
    ).all()
    if any(s.dept_id and s.employee and s.employee.is_driver for s in crew):
        return False
    return sum(1 for s in crew if not s.dept_id) == 1


def find_upcoming_availability(session, emp: Employee, tpl: dict) -> dict | None:
    """The soonest month (next 1-3 months) still accepting availability from members."""
    today = local_today()
    this_month = date(today.year, today.month, 1)
    for i in range(1, 4):
        month_str = add_months(this_month, i).strftime("%Y-%m")
        m = session.query(ScheduleMonth).filter_by(month=month_str).first()
        info = schedule_month_to_dict(m, month_str, tpl)
        if info["availability_open"]:
            sub = session.query(ScheduleSubmission).filter_by(dept_id=emp.dept_id, month=month_str).first()
            return {
                "month": month_str,
                "label": info["label"],
                "deadline": info["availability_deadline"],
                "days_until_deadline": info["days_until_deadline"],
                "submitted": sub is not None,
                "submitted_at": iso_utc(sub.updated_at) if sub else None,
            }
    return None


def active_employees(session) -> list:
    return session.query(Employee).filter_by(active=True).order_by(Employee.dept_id.asc()).all()


def month_state(session, month_str: str, tpl: dict | None = None) -> dict:
    """Fresh hours + coverage for a month (returned after every Builder change)."""
    tpl = tpl or get_staffing_template(session)
    return {
        "hours": compute_schedule_hours(session, month_str, active_employees(session), tpl),
        "coverage": summarize_coverage(load_month_slots(session, month_str, tpl), tpl),
    }


def build_schedule_builder_payload(session, m: ScheduleMonth) -> dict:
    tpl = get_staffing_template(session)
    slots = load_month_slots(session, m.month, tpl)
    employees = active_employees(session)
    first = parse_month(m.month)
    last = month_last_day(first)
    start_hour = template_day_start_hour(tpl)

    avail = load_availability_hours(session, first, last)
    availability = {}
    for dept_id, hours in avail.items():
        days = masks_for_month(hours, first, start_hour)
        if days:
            availability[dept_id] = days
    submissions = {s.dept_id: s.to_dict() for s in session.query(ScheduleSubmission).filter_by(month=m.month).all()}

    # Filled seats in the neighboring months' edge days, for overlap checks at month boundaries
    edge_slots = session.query(ScheduleSlot).filter(
        ScheduleSlot.dept_id.isnot(None),
        ScheduleSlot.month != m.month,
        ScheduleSlot.shift_date >= (first - timedelta(days=1)).isoformat(),
        ScheduleSlot.shift_date <= (last + timedelta(days=1)).isoformat(),
    ).all()

    return {
        "month": schedule_month_to_dict(m, m.month, tpl),
        "template": tpl,
        "slots": [slot_to_dict(s) for s in slots],
        "edge_slots": [slot_to_dict(s) for s in edge_slots],
        "employees": [
            {
                "dept_id": e.dept_id,
                "name": e.name,
                "role": e.role,
                "is_driver": bool(e.is_driver),
                "ot_threshold": e.ot_threshold if e.ot_threshold in (40, 53) else 40,
            }
            for e in employees
        ],
        "avail_start_hour": start_hour,
        "availability": availability,
        "submissions": submissions,
        "hours": compute_schedule_hours(session, m.month, employees, tpl, avail=avail),
        "coverage": summarize_coverage(slots, tpl),
        "autofilled_count": sum(1 for s in slots if s.dept_id and s.assigned_via == "autofill"),
    }


def run_autofill(session, m: ScheduleMonth, tpl: dict, actor: Employee, allow_ot: bool = False) -> dict:
    """
    Fills open future seats from availability. Rules:
      * Only members whose availability covers at least `min_coverage_pct` of a
        seat (partial coverage fills the seat; their hours are what they cover).
      * No double-booking, no stretch longer than MAX_CONTINUOUS_HOURS, no
        overtime unless allow_ot, and never past a member's max-hours request.
      * Every driver-required crew gets a qualified driver first; the last open
        seat on a crew still lacking a driver is held for a driver.
      * Scarcest seats first; among candidates, full coverage beats partial, then
        fewest hours this month, then fewest hours that week.
    """
    first = parse_month(m.month)
    last = month_last_day(first)
    threshold = tpl["min_coverage_pct"] / 100.0
    required = driver_positions(tpl)
    now_abs = local_abs_minutes(local_now())
    employees = active_employees(session)
    emp_by_id = {e.dept_id: e for e in employees}
    avail = load_availability_hours(session, first, last)
    max_hours = {
        s.dept_id: s.max_hours
        for s in session.query(ScheduleSubmission).filter_by(month=m.month).all()
        if s.max_hours is not None
    }

    slots = load_month_slots(session, m.month, tpl)
    crews = defaultdict(list)
    for s in slots:
        crews[group_key(s)].append(s)

    # Existing commitments (this month plus a week either side) for overlap, OT, and rest checks
    intervals = defaultdict(list)
    week_hours = defaultdict(lambda: defaultdict(float))
    month_hours = defaultdict(float)
    first_iso, last_iso = first.isoformat(), last.isoformat()
    nearby = session.query(ScheduleSlot).filter(
        ScheduleSlot.dept_id.isnot(None),
        ScheduleSlot.shift_date >= (first - timedelta(days=8)).isoformat(),
        ScheduleSlot.shift_date <= (last + timedelta(days=8)).isoformat(),
    ).all()
    for s in nearby:
        try:
            ws, we = slot_work_interval(s)
        except ValueError:
            continue
        intervals[s.dept_id].append((ws, we))
        for wk, h in interval_week_segments(ws, we):
            week_hours[s.dept_id][wk] += h
        if first_iso <= s.shift_date <= last_iso:
            month_hours[s.dept_id] += (we - ws) / 60.0

    def has_driver(key) -> bool:
        return any(s.dept_id and emp_by_id.get(s.dept_id) and emp_by_id[s.dept_id].is_driver for s in crews[key])

    def is_open_future(s) -> bool:
        if s.dept_id:
            return False
        try:
            return slot_seat_interval(s)[0] > now_abs
        except ValueError:
            return False

    def candidates(seat, drivers_only=False) -> list:
        try:
            seat_start, seat_end = slot_seat_interval(seat)
        except ValueError:
            return []
        seat_week = interval_week_segments(seat_start, seat_start + 1)[0][0]
        found = []
        for e in employees:
            if drivers_only and not e.is_driver:
                continue
            share, cs, ce = seat_coverage(avail.get(e.dept_id, set()), seat_start, seat_end)
            if share <= 0 or share + 1e-9 < threshold:
                continue
            mine = intervals[e.dept_id]
            if any(cs < oe and os_ < ce for os_, oe in mine):
                continue
            if continuous_block_minutes(mine, cs, ce) > MAX_CONTINUOUS_HOURS * 60:
                continue
            thr = e.ot_threshold if e.ot_threshold in (40, 53) else 40
            if not allow_ot and any(
                week_hours[e.dept_id][wk] + h > thr + 1e-9 for wk, h in interval_week_segments(cs, ce)
            ):
                continue
            cap = max_hours.get(e.dept_id)
            if cap is not None and month_hours[e.dept_id] + (ce - cs) / 60.0 > cap + 1e-9:
                continue
            full = share >= 0.999
            rank = (0 if full else 1, round(month_hours[e.dept_id], 2), round(week_hours[e.dept_id][seat_week], 2), e.dept_id)
            found.append((rank, e, cs, ce, full))
        found.sort(key=lambda c: c[0])
        return found

    def assign(seat, cand) -> None:
        _, e, cs, ce, full = cand
        base = parse_shift_date(seat.shift_date).toordinal() * 1440
        seat.dept_id = e.dept_id
        seat.assigned_via = "autofill"
        seat.assigned_by = actor_label(actor)
        seat.assigned_at = utc_now()
        seat.cover_start_min = None if full else cs - base
        seat.cover_end_min = None if full else ce - base
        intervals[e.dept_id].append((cs, ce))
        for wk, h in interval_week_segments(cs, ce):
            week_hours[e.dept_id][wk] += h
        month_hours[e.dept_id] += (ce - cs) / 60.0

    filled = 0

    # Pass 1: a qualified driver on every driver-required crew that lacks one (scarcest crews first)
    needs_driver = []
    for key, seats in crews.items():
        if key[2] not in required or has_driver(key):
            continue
        opens = sorted((s for s in seats if is_open_future(s)), key=lambda s: s.seat)
        if opens:
            needs_driver.append((key, opens[0]))
    needs_driver.sort(key=lambda kv: (len(candidates(kv[1], drivers_only=True)), kv[0]))
    for key, seat in needs_driver:
        if seat.dept_id or has_driver(key):
            continue
        cands = candidates(seat, drivers_only=True)
        if cands:
            assign(seat, cands[0])
            filled += 1

    # Pass 2: every other open seat, scarcest first
    remaining = [s for s in slots if is_open_future(s)]
    scarcity = {s.id: len(candidates(s)) for s in remaining}
    remaining.sort(key=lambda s: (scarcity[s.id], s.shift_date, s.shift_key, s.position_key, s.seat))
    for seat in remaining:
        if seat.dept_id:
            continue
        key = group_key(seat)
        drivers_only = (
            key[2] in required
            and not has_driver(key)
            and sum(1 for x in crews[key] if not x.dept_id) == 1
        )
        cands = candidates(seat, drivers_only=drivers_only)
        if cands:
            assign(seat, cands[0])
            filled += 1

    session.flush()
    return {
        "filled": filled,
        "open_left": sum(1 for s in slots if is_open_future(s)),
        "driver_gaps": sum(1 for key in crews if key[2] in required and not has_driver(key)),
    }


# ------------------------------------------------------------------------------
# Scheduling Routes
# ------------------------------------------------------------------------------

@app.route("/api/schedule/settings", methods=["GET"])
@require_auth
def get_schedule_settings():
    tpl = get_staffing_template(g.db)
    return jsonify({
        "template": tpl,
        "avail_start_hour": template_day_start_hour(tpl),
        "signup_drop_cutoff_hours": SIGNUP_DROP_CUTOFF_HOURS,
    })


@app.route("/api/schedule/settings", methods=["PUT"])
@require_reviewer
def update_schedule_settings():
    emp = g.current_user
    data = request.get_json(silent=True) or {}
    tpl, err = validate_staffing_template(data.get("template", data))
    if err:
        return jsonify({"error": err}), 400

    payload = json.dumps(tpl)
    row = g.db.get(ScheduleSetting, "staffing_template")
    if row:
        row.value = payload
    else:
        g.db.add(ScheduleSetting(key="staffing_template", value=payload))

    summary = "; ".join(
        f"{s['label']} {s['start']}-{s['end']}: "
        + ", ".join(f"{p['label']} {s['staffing'][p['key']]}" for p in tpl["positions"])
        for s in tpl["shifts"]
    )
    drivers = ", ".join(p["label"] for p in tpl["positions"] if p["requires_driver"]) or "none"
    log_audit(
        g.db,
        actor_id=emp.dept_id,
        actor_name=emp.name,
        action="SCHEDULE_SETTINGS_UPDATED",
        target_type="schedule_settings",
        target_id="staffing_template",
        details=(
            f"Saved staffing levels ({summary}). Driver required: {drivers}. "
            f"Partial availability counts at {tpl['min_coverage_pct']}%+. "
            f"Availability due {tpl['availability_lead_days']} days before each month."
        ),
    )
    g.db.commit()
    return jsonify({"message": "Staffing levels saved.", "template": tpl})


@app.route("/api/schedule/shifts", methods=["GET"])
@require_auth
def list_scheduled_shifts():
    """Published seats held by one member in a date range (used by Log Shift presets)."""
    emp = g.current_user
    dept_id = request.args.get("dept_id", "").strip() or emp.dept_id
    if dept_id != emp.dept_id and not is_reviewer_role(emp):
        return jsonify({"error": "You can only view your own scheduled shifts."}), 403

    try:
        start = parse_shift_date(request.args.get("start", ""))
        end = parse_shift_date(request.args.get("end", "") or start.isoformat())
    except ValueError:
        return jsonify({"error": "Start and end dates must be YYYY-MM-DD."}), 400
    if end < start or (end - start).days > 62:
        return jsonify({"error": "Date range must be between 0 and 62 days."}), 400

    # Include the day before so a partial night shift starting after midnight shows on its work date
    slots = g.db.query(ScheduleSlot).join(
        ScheduleMonth, ScheduleMonth.month == ScheduleSlot.month
    ).filter(
        ScheduleMonth.status == "published",
        ScheduleSlot.dept_id == dept_id,
        ScheduleSlot.shift_date >= (start - timedelta(days=1)).isoformat(),
        ScheduleSlot.shift_date <= end.isoformat(),
    ).order_by(ScheduleSlot.shift_date.asc(), ScheduleSlot.start_time.asc()).all()
    shifts = [slot_to_dict(s) for s in slots]
    shifts = [s for s in shifts if start.isoformat() <= s["work_date"] <= end.isoformat()]
    return jsonify({"shifts": shifts})


@app.route("/api/schedule/<month>", methods=["GET"])
@require_auth
def get_schedule_month(month: str):
    """Member view of a month: published seats, plus one member's availability and hours."""
    emp = g.current_user
    first, err = validate_schedule_month(month, emp)
    if err:
        return jsonify({"error": err}), 400

    target = emp
    req_dept = request.args.get("dept_id", "").strip()
    if req_dept and req_dept != emp.dept_id:
        if not is_reviewer_role(emp):
            return jsonify({"error": "You can only view your own availability."}), 403
        target = g.db.query(Employee).filter_by(dept_id=req_dept).first()
        if not target:
            return jsonify({"error": f"Department ID {req_dept} not found."}), 404

    tpl = get_staffing_template(g.db)
    start_hour = template_day_start_hour(tpl)
    m = g.db.query(ScheduleMonth).filter_by(month=month).first()
    published = m is not None and m.status == "published"
    slots = load_month_slots(g.db, month, tpl) if published else []

    avail = load_availability_hours(g.db, first, month_last_day(first), [target.dept_id])
    submission = g.db.query(ScheduleSubmission).filter_by(dept_id=target.dept_id, month=month).first()
    hours = compute_schedule_hours(g.db, month, [target], tpl, published_only=True, avail=avail)

    return jsonify({
        "month": schedule_month_to_dict(m, month, tpl),
        "template": tpl,
        "published": published,
        "slots": [slot_to_dict(s) for s in slots],
        "coverage": summarize_coverage(slots, tpl) if published else None,
        "member": {
            "dept_id": target.dept_id,
            "name": target.name,
            "is_driver": bool(target.is_driver),
            "ot_threshold": target.ot_threshold if target.ot_threshold in (40, 53) else 40,
        },
        "availability": {
            "start_hour": start_hour,
            "days": masks_for_month(avail.get(target.dept_id, set()), first, start_hour),
        },
        "submission": submission.to_dict() if submission else None,
        "member_hours": hours["members"][0] if hours["members"] else None,
        "weeks": hours["weeks"],
        "upcoming_availability": find_upcoming_availability(g.db, target, tpl),
        "signup_drop_cutoff_hours": SIGNUP_DROP_CUTOFF_HOURS,
        "now": local_now().isoformat(),
    })


@app.route("/api/schedule/<month>", methods=["PUT"])
@require_reviewer
def update_schedule_month(month: str):
    emp = g.current_user
    _, err = validate_schedule_month(month, emp)
    if err:
        return jsonify({"error": err}), 400

    data = request.get_json(silent=True) or {}
    m = get_or_create_schedule_month(g.db, month, actor_label(emp))
    changes = []

    if "availability_deadline" in data:
        try:
            deadline = parse_shift_date(str(data.get("availability_deadline", ""))).isoformat()
        except ValueError:
            return jsonify({"error": "Availability deadline must be a valid date."}), 400
        if deadline != m.availability_deadline:
            changes.append(f"availability deadline {m.availability_deadline} -> {deadline}")
            m.availability_deadline = deadline

    if "notes" in data:
        notes = str(data.get("notes") or "").strip()[:1000]
        if notes != (m.notes or ""):
            changes.append("notes updated")
            m.notes = notes

    if changes:
        log_audit(
            g.db,
            actor_id=emp.dept_id,
            actor_name=emp.name,
            action="SCHEDULE_MONTH_UPDATED",
            target_type="schedule_month",
            target_id=month,
            details=f"Updated {month} schedule: {', '.join(changes)}.",
        )
    g.db.commit()
    return jsonify({
        "message": f"{month_display(month)} schedule updated.",
        "month": schedule_month_to_dict(m, month, get_staffing_template(g.db)),
    })


@app.route("/api/schedule/<month>/availability", methods=["PUT"])
@require_auth
def save_schedule_availability(month: str):
    """
    Replaces a member's hourly availability for a month.
    Body: {"dept_id", "start_hour": 7, "days": {"YYYY-MM-DD": 24-bit mask}, "notes", "max_hours"}
    """
    emp = g.current_user
    reviewer = is_reviewer_role(emp)
    first, err = validate_schedule_month(month, emp)
    if err:
        return jsonify({"error": err}), 400

    data = request.get_json(silent=True) or {}
    target_id = str(data.get("dept_id") or emp.dept_id).strip()
    if target_id != emp.dept_id and not reviewer:
        return jsonify({"error": "You can only submit availability for yourself."}), 403
    target = g.db.query(Employee).filter_by(dept_id=target_id).first()
    if not target:
        return jsonify({"error": f"Department ID {target_id} not found."}), 404

    tpl = get_staffing_template(g.db)
    info = schedule_month_to_dict(g.db.query(ScheduleMonth).filter_by(month=month).first(), month, tpl)
    if not reviewer and not info["availability_open"]:
        if info["status"] == "published":
            msg = f"The {info['label']} schedule is already published. Sign up for open shifts on the Schedule tab instead."
        else:
            msg = (
                f"Availability for {info['label']} closed on {info['availability_deadline']}. "
                "Contact an officer to make changes."
            )
        return jsonify({"error": msg}), 400

    try:
        start_hour = int(data.get("start_hour", template_day_start_hour(tpl)))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid start hour."}), 400
    if not 0 <= start_hour <= 23:
        return jsonify({"error": "Start hour must be between 0 and 23."}), 400

    days_in = data.get("days", {})
    if not isinstance(days_in, dict) or len(days_in) > 31:
        return jsonify({"error": "Invalid availability."}), 400
    first_iso, last_iso = first.isoformat(), month_last_day(first).isoformat()
    days = {}
    for d_str, mask in days_in.items():
        try:
            d_iso = parse_shift_date(str(d_str)).isoformat()
            mask = int(mask)
        except (TypeError, ValueError):
            return jsonify({"error": f"Invalid availability for '{d_str}'."}), 400
        if not first_iso <= d_iso <= last_iso:
            return jsonify({"error": f"{d_iso} is not in {info['label']}."}), 400
        if not 0 <= mask <= FULL_DAY_MASK:
            return jsonify({"error": f"Invalid hours for {d_iso}."}), 400
        if mask:
            days[d_iso] = mask

    notes = str(data.get("notes") or "").strip()[:500]
    max_hours = None
    if data.get("max_hours") not in (None, ""):
        try:
            max_hours = int(float(data.get("max_hours")))
        except (TypeError, ValueError):
            return jsonify({"error": "Max hours must be a number."}), 400
        if not 0 <= max_hours <= 744:
            return jsonify({"error": "Max hours must be between 0 and 744."}), 400

    get_or_create_schedule_month(g.db, month, actor_label(emp))
    g.db.query(ScheduleAvailability).filter_by(dept_id=target.dept_id, month=month).delete(synchronize_session=False)
    for d_iso, mask in sorted(days.items()):
        g.db.add(ScheduleAvailability(
            dept_id=target.dept_id, month=month, op_date=d_iso, start_hour=start_hour, hours_mask=mask
        ))

    sub = g.db.query(ScheduleSubmission).filter_by(dept_id=target.dept_id, month=month).first()
    if sub:
        sub.notes = notes
        sub.max_hours = max_hours
        sub.submitted_by = actor_label(emp)
        sub.updated_at = utc_now()
    else:
        sub = ScheduleSubmission(
            dept_id=target.dept_id,
            month=month,
            notes=notes,
            max_hours=max_hours,
            submitted_by=actor_label(emp),
        )
        g.db.add(sub)

    total_hours = sum(bin(mask).count("1") for mask in days.values())
    on_behalf = f" (entered by {emp.dept_id} {emp.name})" if target.dept_id != emp.dept_id else ""
    log_audit(
        g.db,
        actor_id=emp.dept_id,
        actor_name=emp.name,
        action="AVAILABILITY_SUBMITTED",
        target_type="schedule_availability",
        target_id=f"{target.dept_id}:{month}",
        details=(
            f"{target.dept_id} ({target.name}) availability for {month}: {total_hours} hours across {len(days)} days"
            + (f", max {max_hours}h" if max_hours is not None else "")
            + f"{on_behalf}."
        ),
    )
    g.db.commit()
    return jsonify({
        "message": f"Availability saved for {info['label']} ({total_hours} hours across {len(days)} days).",
        "availability": {"start_hour": start_hour, "days": days},
        "submission": sub.to_dict(),
    })


@app.route("/api/schedule/<month>/builder", methods=["GET"])
@require_reviewer
def get_schedule_builder(month: str):
    emp = g.current_user
    _, err = validate_schedule_month(month, emp)
    if err:
        return jsonify({"error": err}), 400
    m = get_or_create_schedule_month(g.db, month, actor_label(emp))
    g.db.commit()
    return jsonify(build_schedule_builder_payload(g.db, m))


@app.route("/api/schedule/<month>/sync", methods=["POST"])
@require_reviewer
def sync_schedule_month(month: str):
    emp = g.current_user
    _, err = validate_schedule_month(month, emp)
    if err:
        return jsonify({"error": err}), 400
    m = get_or_create_schedule_month(g.db, month, actor_label(emp))
    stats = sync_month_slots(g.db, m, get_staffing_template(g.db))
    message = (
        f"Applied staffing levels to {month_display(month)}: {stats['added']} seats added, "
        f"{stats['removed']} open seats removed"
        + (f", {stats['kept_filled']} filled seats kept (clear them manually if not needed)" if stats["kept_filled"] else "")
        + "."
    )
    log_audit(
        g.db,
        actor_id=emp.dept_id,
        actor_name=emp.name,
        action="SCHEDULE_SYNCED",
        target_type="schedule_month",
        target_id=month,
        details=message,
    )
    g.db.commit()
    return jsonify({"message": message, "stats": stats})


@app.route("/api/schedule/<month>/autofill", methods=["POST"])
@require_reviewer
def autofill_schedule_month(month: str):
    emp = g.current_user
    _, err = validate_schedule_month(month, emp)
    if err:
        return jsonify({"error": err}), 400
    data = request.get_json(silent=True) or {}
    allow_ot = bool(data.get("allow_ot", False))
    m = get_or_create_schedule_month(g.db, month, actor_label(emp))
    tpl = get_staffing_template(g.db)
    result = run_autofill(g.db, m, tpl, emp, allow_ot=allow_ot)

    message = f"Auto-fill placed {result['filled']} people. {result['open_left']} seats are still open"
    if result["driver_gaps"]:
        message += f", and {result['driver_gaps']} station shifts still need a driver"
    message += "."
    log_audit(
        g.db,
        actor_id=emp.dept_id,
        actor_name=emp.name,
        action="SCHEDULE_AUTOFILLED",
        target_type="schedule_month",
        target_id=month,
        details=f"{message} (overtime {'allowed' if allow_ot else 'not allowed'})",
    )
    g.db.commit()
    return jsonify({"message": message, **result})


@app.route("/api/schedule/<month>/autofill/clear", methods=["POST"])
@require_reviewer
def clear_autofill_schedule_month(month: str):
    emp = g.current_user
    _, err = validate_schedule_month(month, emp)
    if err:
        return jsonify({"error": err}), 400
    seats = g.db.query(ScheduleSlot).filter_by(month=month, assigned_via="autofill").all()
    for s in seats:
        s.dept_id = None
        s.assigned_via = None
        s.assigned_by = None
        s.assigned_at = None
        s.cover_start_min = None
        s.cover_end_min = None
    message = f"Cleared {len(seats)} auto-filled seats in {month_display(month)}."
    if seats:
        log_audit(
            g.db,
            actor_id=emp.dept_id,
            actor_name=emp.name,
            action="SCHEDULE_AUTOFILL_CLEARED",
            target_type="schedule_month",
            target_id=month,
            details=message,
        )
    g.db.commit()
    return jsonify({"message": message, "cleared": len(seats)})


@app.route("/api/schedule/<month>/publish", methods=["POST"])
@require_reviewer
def publish_schedule_month(month: str):
    emp = g.current_user
    _, err = validate_schedule_month(month, emp)
    if err:
        return jsonify({"error": err}), 400
    m = g.db.query(ScheduleMonth).filter_by(month=month).first()
    if not m:
        return jsonify({"error": "Open this month in the Builder before publishing."}), 404

    data = request.get_json(silent=True) or {}
    publish = bool(data.get("published", True))
    tpl = get_staffing_template(g.db)
    coverage = summarize_coverage(load_month_slots(g.db, month, tpl), tpl)

    if publish:
        m.status = "published"
        m.published_at = utc_now()
        m.published_by = actor_label(emp)
        action = "SCHEDULE_PUBLISHED"
        driver_note = (
            f", {coverage['drivers']['missing']} station shifts without a driver" if coverage["drivers"]["missing"] else ""
        )
        details = (
            f"Published {month} schedule ({coverage['filled']}/{coverage['total']} seats filled, "
            f"{coverage['open']} open for sign-up{driver_note})."
        )
        message = f"{month_display(month)} schedule published. {coverage['open']} open shifts are available for sign-up."
    else:
        m.status = "draft"
        action = "SCHEDULE_UNPUBLISHED"
        details = f"Returned {month} schedule to draft."
        message = f"{month_display(month)} schedule returned to draft. Members can no longer see it."

    log_audit(
        g.db,
        actor_id=emp.dept_id,
        actor_name=emp.name,
        action=action,
        target_type="schedule_month",
        target_id=month,
        details=details,
    )
    g.db.commit()
    return jsonify({"message": message, "month": schedule_month_to_dict(m, month, tpl)})


@app.route("/api/schedule/<month>/slots", methods=["POST"])
@require_reviewer
def add_schedule_seat(month: str):
    """Adds one seat to a shift/position on one date. Body: {shift_date, shift_key, position_key}"""
    emp = g.current_user
    first, err = validate_schedule_month(month, emp)
    if err:
        return jsonify({"error": err}), 400
    data = request.get_json(silent=True) or {}
    shift_key = str(data.get("shift_key", "")).strip()
    position_key = str(data.get("position_key", "")).strip()
    try:
        shift_date = parse_shift_date(str(data.get("shift_date", ""))).isoformat()
    except ValueError:
        return jsonify({"error": "A valid shift date is required."}), 400
    if not first.isoformat() <= shift_date <= month_last_day(first).isoformat():
        return jsonify({"error": f"{shift_date} is not in {month_display(month)}."}), 400

    m = get_or_create_schedule_month(g.db, month, actor_label(emp))
    tpl = get_staffing_template(g.db)
    sh = next((s for s in tpl["shifts"] if s["key"] == shift_key), None)
    pos = next((p for p in tpl["positions"] if p["key"] == position_key), None)
    month_seats = g.db.query(ScheduleSlot).filter_by(month=m.month, shift_key=shift_key, position_key=position_key).all()
    crew = [s for s in month_seats if s.shift_date == shift_date]
    sample = crew[0] if crew else (month_seats[0] if month_seats else None)

    if sh and pos:
        meta = (sh["label"], pos["label"], sh["start"], sh["end"])
    elif sample:
        meta = (sample.shift_label, sample.position_label, sample.start_time, sample.end_time)
    else:
        return jsonify({"error": "Unknown shift or position."}), 400
    if len(crew) >= 10:
        return jsonify({"error": "That shift already has the maximum of 10 seats."}), 400

    slot = ScheduleSlot(
        month=m.month,
        shift_date=shift_date,
        shift_key=shift_key,
        shift_label=meta[0],
        position_key=position_key,
        position_label=meta[1],
        seat=max((s.seat for s in crew), default=0) + 1,
        start_time=meta[2],
        end_time=meta[3],
    )
    g.db.add(slot)
    g.db.flush()
    log_audit(
        g.db,
        actor_id=emp.dept_id,
        actor_name=emp.name,
        action="SCHEDULE_SEAT_ADDED",
        target_type="schedule_slot",
        target_id=str(slot.id),
        details=f"Added seat {slot.seat} to {shift_date} {meta[0]} ({meta[1]}).",
    )
    g.db.commit()
    return jsonify({"message": "Seat added.", "slot": slot_to_dict(slot), **month_state(g.db, m.month, tpl)}), 201


@app.route("/api/schedule/slots/<int:slot_id>", methods=["DELETE"])
@require_reviewer
def remove_schedule_seat(slot_id: int):
    emp = g.current_user
    slot = g.db.get(ScheduleSlot, slot_id)
    if not slot:
        return jsonify({"error": "Schedule seat not found."}), 404
    force = request.args.get("force", "").lower() == "true"
    if slot.dept_id and not force:
        name = slot.employee.name if slot.employee else slot.dept_id
        return jsonify({"error": f"This seat is filled by {name}. Clear it first or confirm removal."}), 409

    month = slot.month
    details = f"Removed seat {slot.seat} from {slot.shift_date} {slot.shift_label} ({slot.position_label})"
    if slot.dept_id:
        details += f", unassigning {slot.dept_id} ({slot.employee.name if slot.employee else 'unknown'})"
    g.db.delete(slot)
    log_audit(
        g.db,
        actor_id=emp.dept_id,
        actor_name=emp.name,
        action="SCHEDULE_SEAT_REMOVED",
        target_type="schedule_slot",
        target_id=str(slot_id),
        details=details + ".",
    )
    g.db.commit()
    return jsonify({"message": "Seat removed.", "deleted_id": slot_id, **month_state(g.db, month)})


@app.route("/api/schedule/slots/<int:slot_id>", methods=["PUT"])
@require_reviewer
def assign_schedule_slot(slot_id: int):
    emp = g.current_user
    slot = g.db.get(ScheduleSlot, slot_id)
    if not slot:
        return jsonify({"error": "Schedule seat not found."}), 404

    data = request.get_json(silent=True) or {}
    new_dept = str(data.get("dept_id") or "").strip() or None
    old_dept = slot.dept_id
    old_name = slot.employee.name if slot.employee else None

    if new_dept != old_dept:
        if new_dept:
            target = g.db.query(Employee).filter_by(dept_id=new_dept).first()
            if not target or not target.active:
                return jsonify({"error": f"Department ID {new_dept} is not an active member."}), 400
            # If they're only available for part of the seat, they work that part; the seat still counts as filled
            first = parse_month(slot.month)
            avail = load_availability_hours(g.db, first, month_last_day(first), [new_dept])
            cover_start, cover_end = cover_for_member(avail.get(new_dept, set()), slot)
            if cover_start is not None:
                base = parse_shift_date(slot.shift_date).toordinal() * 1440
                work = (base + cover_start, base + cover_end)
            else:
                work = slot_seat_interval(slot)
            conflict = find_schedule_conflict(g.db, new_dept, slot, *work)
            if conflict:
                return jsonify({"error": f"{target.name} is already scheduled {describe_slot(conflict)}."}), 409
            slot.dept_id = new_dept
            slot.assigned_via = "officer"
            slot.assigned_by = actor_label(emp)
            slot.assigned_at = utc_now()
            slot.cover_start_min = cover_start
            slot.cover_end_min = cover_end
            action = "SCHEDULE_ASSIGNED"
            details = f"Assigned {new_dept} ({target.name}) to {describe_slot(slot)}"
            details += f", replacing {old_dept} ({old_name})." if old_dept else "."
            message = f"{target.name} assigned."
        else:
            slot.dept_id = None
            slot.assigned_via = None
            slot.assigned_by = None
            slot.assigned_at = None
            slot.cover_start_min = None
            slot.cover_end_min = None
            action = "SCHEDULE_UNASSIGNED"
            details = f"Removed {old_dept} ({old_name}) from {describe_slot(slot)}."
            message = "Seat cleared."

        log_audit(
            g.db,
            actor_id=emp.dept_id,
            actor_name=emp.name,
            action=action,
            target_type="schedule_slot",
            target_id=str(slot.id),
            details=details,
        )
        g.db.commit()
    else:
        message = "No change."

    return jsonify({"message": message, "slot": slot_to_dict(slot), **month_state(g.db, slot.month)})


@app.route("/api/schedule/slots/<int:slot_id>/signup", methods=["POST"])
@require_auth
def signup_schedule_slot(slot_id: int):
    emp = g.current_user
    slot = g.db.get(ScheduleSlot, slot_id)
    if not slot:
        return jsonify({"error": "Schedule seat not found."}), 404

    m = g.db.query(ScheduleMonth).filter_by(month=slot.month).first()
    if not m or m.status != "published":
        return jsonify({"error": "This schedule hasn't been published yet."}), 400
    if slot_start_local(slot) <= local_now():
        return jsonify({"error": "This shift has already started."}), 400
    if slot.dept_id:
        if slot.dept_id == emp.dept_id:
            return jsonify({"error": "You're already signed up for this shift."}), 409
        return jsonify({"error": "This shift was just filled by someone else."}), 409
    if not emp.is_driver and seat_reserved_for_driver(g.db, slot, get_staffing_template(g.db)):
        return jsonify({"error": "This seat is held for a qualified driver so the crew has one."}), 409

    conflict = find_schedule_conflict(g.db, emp.dept_id, slot, *slot_seat_interval(slot))
    if conflict:
        return jsonify({"error": f"You're already scheduled {describe_slot(conflict)}."}), 409

    # Conditional update so two people can't claim the same open seat at once
    claimed = g.db.query(ScheduleSlot).filter(
        ScheduleSlot.id == slot.id,
        ScheduleSlot.dept_id.is_(None),
    ).update(
        {
            ScheduleSlot.dept_id: emp.dept_id,
            ScheduleSlot.assigned_via: "signup",
            ScheduleSlot.assigned_by: actor_label(emp),
            ScheduleSlot.assigned_at: utc_now(),
            ScheduleSlot.cover_start_min: None,
            ScheduleSlot.cover_end_min: None,
        },
        synchronize_session=False,
    )
    if not claimed:
        g.db.rollback()
        return jsonify({"error": "This shift was just filled by someone else."}), 409

    log_audit(
        g.db,
        actor_id=emp.dept_id,
        actor_name=emp.name,
        action="VACANCY_SIGNUP",
        target_type="schedule_slot",
        target_id=str(slot.id),
        details=f"{emp.dept_id} ({emp.name}) signed up for {describe_slot(slot)}.",
    )
    g.db.commit()
    g.db.refresh(slot)
    return jsonify({
        "message": f"You're signed up: {slot.shift_label} {slot.start_time}–{slot.end_time}, {slot.position_label}.",
        "slot": slot_to_dict(slot),
    })


@app.route("/api/schedule/slots/<int:slot_id>/drop", methods=["POST"])
@require_auth
def drop_schedule_slot(slot_id: int):
    emp = g.current_user
    slot = g.db.get(ScheduleSlot, slot_id)
    if not slot:
        return jsonify({"error": "Schedule seat not found."}), 404
    if slot.dept_id != emp.dept_id:
        return jsonify({"error": "You can only drop your own shifts."}), 403
    if slot.assigned_via != "signup":
        return jsonify({"error": "This shift was assigned by an officer. Contact an officer to change it."}), 400
    if slot_start_local(slot) - local_now() < timedelta(hours=SIGNUP_DROP_CUTOFF_HOURS):
        return jsonify({
            "error": f"Sign-ups can only be dropped more than {SIGNUP_DROP_CUTOFF_HOURS} hours before the shift. Contact an officer."
        }), 400

    slot.dept_id = None
    slot.assigned_via = None
    slot.assigned_by = None
    slot.assigned_at = None
    slot.cover_start_min = None
    slot.cover_end_min = None
    log_audit(
        g.db,
        actor_id=emp.dept_id,
        actor_name=emp.name,
        action="VACANCY_DROPPED",
        target_type="schedule_slot",
        target_id=str(slot.id),
        details=f"{emp.dept_id} ({emp.name}) dropped their sign-up for {describe_slot(slot)}.",
    )
    g.db.commit()
    return jsonify({"message": "You've been removed from that shift. It's open for sign-up again.", "slot": slot_to_dict(slot)})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug_mode = os.environ.get("FLASK_DEBUG", "false").lower() in ("true", "1")
    app.run(host="0.0.0.0", port=port, debug=debug_mode)
