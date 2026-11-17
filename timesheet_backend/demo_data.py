"""
Riverview FD Timesheet & Scheduling - local DEMO data
-----------------------------------------------------
Builds a throwaway SQLite database (demo.db, git-ignored) filled with a FAKE
roster and realistic schedule data so you can click around the portal.
It never touches timesheet.db or any production database.

    python demo_data.py           # rebuild demo.db
    python demo_data.py --serve   # rebuild, then run the API on http://127.0.0.1:5000

Then serve the site from the repo root (python -m http.server 8080) and open
http://localhost:8080/timesheet.html. Every demo account's PIN is 1234.

What you get (dates are relative to today):
  * This month: published, mostly filled (some partial shifts and open seats to
    sign up for), plus a few logged and approved timesheet hours.
  * Next month: availability from most members (varied hourly patterns), the
    deadline kept open, and no seats filled yet, ready for the Builder / Auto-Fill.
"""

import os
import sys
from datetime import timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
DEMO_DB = os.path.join(HERE, "demo.db")

# FAKE people only. Columns match the real roster CSV (plus an optional DRIVER column).
DEMO_ROSTER = """AGENCY PERSONNEL ID,LAST NAME,FIRST NAME,RANK,EMPLOYMENT STATUS,DRIVER
960,Alvarez,Jordan,Engineer,Full Time,Yes
961,Brooks,Casey,Firefighter,Part Time,No
962,Chen,Riley,Engineer,Part Time,Yes
963,Dawson,Morgan,Firefighter,Part Time,No
964,Ellis,Taylor,Engineer,Full Time,Yes
965,Foster,Avery,Firefighter,Part Time,No
966,Grant,Quinn,Engineer,Part Time,Yes
967,Hayes,Parker,Firefighter,Part Time,No
968,Irwin,Skyler,Firefighter,Part Time,Yes
969,Jensen,Rowan,Engineer,Full Time,Yes
970,Kim,Devon,Captain,Full Time,Yes
971,Lopez,Harper,Firefighter,Part Time,No
972,Moore,Emerson,Firefighter,Part Time,No
973,Nash,Finley,Engineer,Part Time,Yes
974,Owens,Reese,Firefighter,Part Time,No
975,Price,Sage,Firefighter,Part Time,No
"""


def build():
    if os.path.exists(DEMO_DB):
        os.remove(DEMO_DB)
    os.environ["SQLITE_DB_PATH"] = DEMO_DB
    os.environ["ROSTER_CSV"] = DEMO_ROSTER
    os.environ.setdefault("SECRET_KEY", "rvfd-local-demo-only")
    os.environ["ENTRY_MAX_PER_MINUTE"] = "100000"
    os.environ["ENTRY_MAX_PER_HOUR"] = "100000"
    os.environ.pop("DATABASE_URL", None)
    os.environ.pop("POSTGRES_URI", None)
    sys.path.insert(0, HERE)
    import app as A

    c = A.app.test_client()

    def login(dept_id):
        r = c.post("/api/auth/login", json={"dept_id": dept_id, "pin": "1234"})
        assert r.status_code == 200, r.json
        return {"Authorization": "Bearer " + r.json["token"]}

    off = login("953")
    members = [str(i) for i in range(960, 976)]
    today = A.local_today()
    this_month = A.date(today.year, today.month, 1)
    cur = this_month.strftime("%Y-%m")
    nxt = A.add_months(this_month, 1).strftime("%Y-%m")

    def bits(a, b):  # grid rows relative to 07:00 (row 0 = 07:00, row 10 = 17:00, row 23 = 06:00)
        return sum(1 << i for i in range(a, b))

    DAY, NIGHT = bits(0, 10), bits(10, 24)
    patterns = {
        # dept: function(day_index 0.., weekday 0=Mon) -> mask
        "960": lambda n, wd: DAY | NIGHT if n % 3 == 0 else 0,                       # 24/48 style
        "961": lambda n, wd: 0,                                                      # left blank to try yourself
        "962": lambda n, wd: DAY if wd < 5 else 0,                                   # weekday days
        "963": lambda n, wd: bits(1, 10) if wd in (1, 3) else (DAY if wd == 5 else 0),  # Tue/Thu from 08:00
        "964": lambda n, wd: DAY | NIGHT if n % 3 == 1 else 0,
        "965": lambda n, wd: NIGHT if wd in (4, 5) else 0,                           # Fri/Sat nights
        "966": lambda n, wd: DAY if wd >= 5 else (NIGHT if wd == 2 else 0),          # weekends + Wed night
        "967": lambda n, wd: bits(2, 10) if wd < 5 and n % 2 == 0 else 0,           # from 09:00
        "968": lambda n, wd: NIGHT if n % 2 == 1 else 0,
        "969": lambda n, wd: DAY | NIGHT if n % 3 == 2 else 0,
        "970": lambda n, wd: DAY if wd < 4 else 0,
        "971": lambda n, wd: bits(0, 8) if wd in (0, 2, 4) else 0,                   # leaves at 15:00
        "972": lambda n, wd: DAY if wd in (5, 6) else (NIGHT if wd == 0 else 0),
        "973": lambda n, wd: NIGHT if wd in (1, 3, 6) else (DAY if wd == 2 else 0),
        "974": lambda n, wd: DAY if n % 4 in (0, 1) else 0,
        "975": lambda n, wd: 0,                                                      # hasn't responded
    }
    notes = {"963": "Can't start before 8 on Tue/Thu", "967": "School drop-off, can start at 9",
             "971": "Need to leave by 3pm", "965": "Prefer Station 2"}
    max_hours = {"974": 96, "962": 120}

    def availability(month_str):
        out = {}
        for m in members:
            days = {}
            for n, d in enumerate(A.iter_month_dates(A.parse_month(month_str))):
                mask = patterns[m](n, d.weekday())
                if mask:
                    days[d.isoformat()] = mask
            out[m] = days
        return out

    # ---- This month: availability, filled by hand (like an officer would), published ----
    for m, days in availability(cur).items():
        if m == "975":
            continue
        r = c.put(f"/api/schedule/{cur}/availability", headers=off,
                  json={"dept_id": m, "days": days, "notes": notes.get(m, ""), "max_hours": max_hours.get(m)})
        assert r.status_code == 200, r.json
    b = c.get(f"/api/schedule/{cur}/builder", headers=off).json
    start_hour = b["avail_start_hour"]
    avail = {}
    for m, days in b["availability"].items():
        hs = set()
        for d, mask in days.items():
            base = A.parse_shift_date(d).toordinal() * 24 + start_hour
            hs |= {base + i for i in range(24) if mask >> i & 1}
        avail[m] = hs
    drivers = {e["dept_id"] for e in b["employees"] if e["is_driver"]}
    hours = {m: 0.0 for m in members}
    slots = sorted(b["slots"], key=lambda s: (s["shift_date"], s["shift_key"], s["position_key"], s["seat"]))
    crew_has_driver = set()
    for n, s in enumerate(slots):
        if n % 8 == 5:
            continue  # leave some vacancies for sign-up
        crew = (s["shift_date"], s["shift_key"], s["position_key"])
        need_driver = s["position_key"] in ("st1", "st2") and crew not in crew_has_driver
        seat = A.get_shift_interval_minutes(s["shift_date"], s["start_time"], s["end_time"])
        order = sorted(members, key=lambda x: (0 if (need_driver and x in drivers) else 1, hours[x]))
        for m in order:
            if A.seat_coverage(avail.get(m, set()), *seat)[0] < 0.75:
                continue
            r = c.put(f"/api/schedule/slots/{s['id']}", headers=off, json={"dept_id": m})
            if r.status_code == 200:
                hours[m] += r.json["slot"]["hours"]
                if m in drivers:
                    crew_has_driver.add(crew)
                break
    c.post(f"/api/schedule/{cur}/publish", headers=off, json={"published": True})

    # ---- A few logged timesheet hours for past scheduled shifts (some approved) ----
    published = c.get(f"/api/schedule/{cur}", headers=off).json["slots"]
    logged = 0
    for m in members:
        past = [s for s in published if s["dept_id"] == m and s["work_date"] < today.isoformat()][-3:]
        for i, s in enumerate(past):
            r = c.post("/api/entries", headers=off, json={
                "dept_id": m, "shift_date": s["work_date"], "start_time": s["work_start"],
                "end_time": s["work_end"], "notes": f"Scheduled: {s['shift_label']} - {s['position_label']}",
            })
            if r.status_code == 201:
                logged += 1
                if i < 2:
                    for e in r.json["entries"]:
                        c.post(f"/api/entries/{e['id']}/review", headers=off, json={"status": "approved"})

    # ---- Next month: availability from most members, deadline kept open, nothing filled ----
    c.get(f"/api/schedule/{nxt}/builder", headers=off)  # creates the month and its seats
    deadline = A.default_availability_deadline(A.parse_month(nxt), 14)
    if deadline < (today + timedelta(days=3)).isoformat():
        deadline = (today + timedelta(days=7)).isoformat()
        c.put(f"/api/schedule/{nxt}", headers=off, json={"availability_deadline": deadline})
    for m, days in availability(nxt).items():
        if m in ("961", "975", "972"):
            continue  # not submitted yet (961 is yours to try)
        r = c.put(f"/api/schedule/{nxt}/availability", headers=off,
                  json={"dept_id": m, "days": days, "notes": notes.get(m, ""), "max_hours": max_hours.get(m)})
        assert r.status_code == 200, r.json

    cov = c.get(f"/api/schedule/{cur}/builder", headers=off).json["coverage"]
    print(f"Demo database ready: {DEMO_DB}")
    print(f"  {A.month_display(cur)}: published, {cov['filled']}/{cov['total']} seats filled, {logged} timesheet entries")
    print(f"  {A.month_display(nxt)}: availability from 13 of 17 members, due {deadline}, ready for Auto-Fill")
    print("  Sign in with PIN 1234:  953 = officer (Builder)   961 = member who hasn't entered availability")
    print("                          960 = full-time driver    963 = member with partial (08:00) availability")
    return A


if __name__ == "__main__":
    demo_app = build()
    if "--serve" in sys.argv:
        print("Serving the demo API on http://127.0.0.1:5000 (Ctrl+C to stop)")
        demo_app.app.run(host="127.0.0.1", port=5000, debug=False)
