"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real environment variables win)

The assignment asks for a single file, so it is split into numbered sections:
  1. Configuration and database connection
  2. Time helpers            (epoch ms <-> datetime, IST, attendance date R1)
  3. Business rules          (R2 - R5)
  4. Request models          (input validation)
  5. Serialisers             (MongoDB document -> API response)
  6. Indexes and start-up
  7. Endpoints: system, employees, attendance
  8. Analytics pipelines     (one builder per endpoint, shared with explain)
  9. Endpoints: analytics
 10. Admin: explain
"""
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from bson import Decimal128, json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse
from pydantic import AfterValidator, BaseModel, Field, StrictInt, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

log = logging.getLogger("attendance")


# --------------------------------------------------------------------------- #
# 1. Configuration and database connection
# --------------------------------------------------------------------------- #
load_dotenv()  # does not override variables that are already set, so real env vars win

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,                  # return UTC-aware datetimes instead of naive ones
    serverSelectionTimeoutMS=5000,  # fail fast (health -> 503) instead of hanging for 30 s
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]  # R6
GRACE_SECONDS = 10 * 60                            # R2
MIN_OVERTIME_MINUTES = 30                          # R3
HALF_DAY_BELOW_HOURS = 4.50                        # R5
MAX_PUNCH_SPAN = timedelta(hours=24)               # punch_out must be within 24 h of punch_in

MIN_EPOCH_MS = 100_000_000_000    # anything smaller looks like seconds
MAX_EPOCH_MS = 4_102_444_800_000  # 2100-01-01


# --------------------------------------------------------------------------- #
# 2. Time helpers
# --------------------------------------------------------------------------- #
def to_ms(dt: Optional[datetime]) -> Optional[int]:
    """BSON datetime -> epoch milliseconds. Integer maths, so no float rounding errors."""
    if dt is None:
        return None
    if dt.tzinfo is None:  # defensive: BSON dates are UTC, so a naive value is UTC
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def from_ms(ms: int) -> datetime:
    """Epoch milliseconds -> UTC datetime, truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def now_utc() -> datetime:
    """Current instant, truncated to whole seconds (R1)."""
    return datetime.now(UTC).replace(microsecond=0)


def parse_hhmm(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def is_overnight(emp: dict) -> bool:
    return parse_hhmm(emp["shift_end"]) <= parse_hhmm(emp["shift_start"])


def attendance_date(punch_in: datetime, emp: dict) -> date:
    """R1: the IST calendar date of the punch-in. For an overnight shift, a punch-in whose IST
    time is earlier than shift_end belongs to the shift that started the previous day."""
    local = punch_in.astimezone(IST)
    day = local.date()
    if is_overnight(emp) and local.time() < parse_hhmm(emp["shift_end"]):
        day -= timedelta(days=1)
    return day


def shift_bounds(day: date, emp: dict) -> tuple[datetime, datetime]:
    """Start and end instants of the employee's shift that starts on `day` (IST).
    An overnight shift ends on the next calendar day."""
    start = datetime.combine(day, parse_hhmm(emp["shift_start"]), tzinfo=IST)
    end = datetime.combine(day, parse_hhmm(emp["shift_end"]), tzinfo=IST)
    if end <= start:
        end += timedelta(days=1)
    return start, end


# --------------------------------------------------------------------------- #
# 3. Business rules R2 - R5
# --------------------------------------------------------------------------- #
def round_half_up(value: Decimal, places: int) -> Decimal:
    """R8: 2.675 -> 2.68. Python's round() uses banker's rounding on an inexact float."""
    return value.quantize(Decimal(10) ** -places, rounding=ROUND_HALF_UP)


def compute_late_minutes(punch_in: datetime, shift_start: datetime) -> int:
    """R2: late only when strictly more than 10:00 after shift start; then the whole minutes
    since shift start (not since the end of the grace period)."""
    seconds = int((punch_in - shift_start).total_seconds())
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def compute_overtime_minutes(punch_out: datetime, shift_end: datetime) -> int:
    """R3: whole minutes after shift end, counted only when at least 30."""
    minutes = int((punch_out - shift_end).total_seconds()) // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: hours worked, 2 decimals half-up. Seconds are whole (R1), so Decimal maths is exact."""
    seconds = int((punch_out - punch_in).total_seconds())
    return float(round_half_up(Decimal(seconds) / 3600, 2))


def punch_out_fields(punch_in: datetime, punch_out: datetime, day: date, emp: dict) -> dict:
    """R3 - R5: the fields computed when a record gets its punch-out."""
    _, shift_end = shift_bounds(day, emp)
    work_hours = compute_work_hours(punch_in, punch_out)
    return {
        "work_hours": work_hours,
        "overtime_minutes": compute_overtime_minutes(punch_out, shift_end),
        "half_day": work_hours < HALF_DAY_BELOW_HOURS,  # R5 uses the rounded value
    }


def derive_fields(status: str, punch_in: Optional[datetime], punch_out: Optional[datetime],
                  day: date, emp: dict) -> dict:
    """All four computed fields of a record (table in DATA_MODEL.md section 3)."""
    fields = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if status not in PRESENCE_STATUSES:  # ABSENT / LEAVE: no punch times, all defaults
        return fields
    shift_start, _ = shift_bounds(day, emp)
    fields["late_minutes"] = compute_late_minutes(punch_in, shift_start)
    if punch_out is not None:
        fields.update(punch_out_fields(punch_in, punch_out, day, emp))
    return fields


# --------------------------------------------------------------------------- #
# 4. Request models and validated types
# --------------------------------------------------------------------------- #
def _valid_date(value: str) -> str:
    """'YYYY-MM-DD' that is also a real calendar day (2026-02-30 is rejected)."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("expected a date as YYYY-MM-DD")
    date.fromisoformat(value)
    return value


# Strict: floats, strings and booleans are rejected, as are seconds-looking values.
EpochMs = Annotated[StrictInt, Field(ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)]
DateStr = Annotated[str, AfterValidator(_valid_date)]
HHMM = Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]

# Shared query parameters (R10)
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]


class EmployeeCreate(BaseModel):
    emp_code: Annotated[str, Field(pattern=r"^EMP\d{4,6}$")]
    name: Annotated[str, Field(min_length=1, max_length=100)]
    email: Annotated[str, Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    department: Annotated[str, Field(min_length=1, max_length=50)]
    shift_start: HHMM = "09:30"
    shift_end: HHMM = "18:30"
    joined_on: DateStr

    @model_validator(mode="after")
    def shift_times_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None  # defaults to now
    status: PresenceStatus = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None  # defaults to now


class RegularizeRequest(BaseModel):
    """Only these fields can be corrected. Derived fields sent by a client are ignored
    (Pydantic drops unknown keys by default)."""
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: Annotated[str, Field(min_length=5, max_length=200)]
    regularized_by: Annotated[str, Field(min_length=1, max_length=50)]

    @model_validator(mode="after")
    def no_explicit_nulls(self):
        # The contract types these as non-nullable: omit a field to keep it, never send null.
        for name in ("status", "punch_in", "punch_out"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} can not be null; omit it to keep the stored value")
        return self


def unprocessable(message: str, loc: tuple = ("body",)) -> RequestValidationError:
    """A 422 with the same body shape FastAPI uses for its own validation errors."""
    return RequestValidationError([{"type": "value_error", "loc": loc, "msg": message, "input": None}])


def check_punch_out(punch_in: datetime, punch_out: datetime, loc: tuple) -> None:
    if punch_out <= punch_in:
        raise unprocessable("punch_out must be after punch_in", loc)
    if punch_out - punch_in > MAX_PUNCH_SPAN:
        raise unprocessable("punch_out must be within 24 hours of punch_in", loc)


# --------------------------------------------------------------------------- #
# 5. Serialisers: MongoDB document -> API response (no _id, instants as epoch ms)
# --------------------------------------------------------------------------- #
EMPLOYEE_FIELDS = ("emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on")


def employee_out(doc: dict) -> dict:
    out = {field: doc.get(field) for field in EMPLOYEE_FIELDS}
    out["created_at"] = to_ms(doc.get("created_at"))
    return out


def history_out(entry: dict) -> dict:
    changes = {}
    for field, change in (entry.get("changes") or {}).items():
        if field in ("punch_in", "punch_out"):  # nested BSON dates -> epoch ms as well
            change = {"from": to_ms(change.get("from")), "to": to_ms(change.get("to"))}
        changes[field] = change
    return {"at": to_ms(entry.get("at")), "by": entry.get("by"), "reason": entry.get("reason"),
            "changes": changes}


def record_out(doc: dict) -> dict:
    """Missing derived fields mean their defaults (legacy records, DATA_MODEL.md sample 9)."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": [history_out(entry) for entry in doc.get("history") or []],
    }


def page_out(items: list, total: int, page: int, page_size: int) -> dict:
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# 6. Indexes and start-up
# --------------------------------------------------------------------------- #
def create_indexes() -> None:
    """Idempotent: create_index is a no-op when the same index already exists."""
    # Identity of an employee; also makes concurrent duplicate creates fail with 409.
    db.employees.create_index("emp_code", unique=True, name="uniq_emp_code")
    # List employees of a department sorted by emp_code; department lookups for analytics.
    db.employees.create_index([("department", 1), ("emp_code", 1)], name="department_emp_code")
    # Department summary headcount: employees with joined_on <= end of month (R9).
    db.employees.create_index("joined_on", name="joined_on")
    # One record per (emp_code, date): blocks the punch-in race, and serves every per-employee
    # date-range query (monthly, punch-out, PATCH, the $lookup in the department summary).
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True, name="uniq_emp_code_date")
    # Attendance list sorted by date desc then emp_code; date-range scans (leaderboard).
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)], name="date_desc_emp_code")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        create_indexes()
    except PyMongoError as exc:  # keep starting: /health reports the database problem
        log.error("could not create indexes: %s", exc)
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
    docs_url=None,  # the interactive docs are served by docs_page() at "/" (dark theme)
)


# --------------------------------------------------------------------------- #
# 7. Endpoints: system, employees, attendance
# --------------------------------------------------------------------------- #
def get_employee(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


@app.get("/", include_in_schema=False)
def docs_page() -> HTMLResponse:
    """Interactive docs (Swagger UI) at the server address. Not part of the contract.
    Swagger UI ships a dark theme that turns on when <html> has the class "dark-mode"."""
    page = get_swagger_ui_html(
        openapi_url=app.openapi_url,
        title=f"{app.title} - Swagger UI",
        swagger_ui_parameters={"deepLinking": False},  # keep the address bar at "/" (no "#/...")
    )
    return HTMLResponse(page.body.decode().replace("<html>", '<html class="dark-mode">', 1))


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)  # the unique index decides, not a find-then-insert
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: Page = 1, page_size: PageSize = 20):
    query = {} if department is None else {"department": department}
    total = db.employees.count_documents(query)
    cursor = (db.employees.find(query)
              .sort("emp_code", 1)
              .skip((page - 1) * page_size)
              .limit(page_size))
    return page_out([employee_out(doc) for doc in cursor], total, page, page_size)


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = get_employee(body.emp_code)
    punched_at = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    day = attendance_date(punched_at, emp)
    doc = {
        "emp_code": emp["emp_code"],
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        **derive_fields(body.status, punched_at, None, day, emp),
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date): exactly one racer wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = get_employee(body.emp_code)
    punched_at = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    # The most recent record whose punch_in is at or before punched_at. Searching by punch_in
    # (not by today's date) is what makes an overnight shift work across midnight. For one
    # employee, `date` order equals `punch_in` order (R1), so the (emp_code, date) index
    # provides the sort and MongoDB stops at the first match.
    record = db.attendance_logs.find_one(
        {"emp_code": emp["emp_code"], "punch_in": {"$lte": punched_at}},
        sort=[("date", -1)],
    )
    if record is None:
        raise HTTPException(404, "no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    check_punch_out(record["punch_in"], punched_at, ("body", "punched_at"))

    day = date.fromisoformat(record["date"])
    update = {"punch_out": punched_at, **punch_out_fields(record["punch_in"], punched_at, day, emp)}
    # Atomic compare-and-set: the update only matches while punch_out is still null, so of
    # two simultaneous punch-outs exactly one modifies the record and the other gets 409.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": update},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


ATTENDANCE_SORT = [("date", -1), ("emp_code", 1)]


def attendance_filter(emp_code: Optional[str], date_from: Optional[str], date_to: Optional[str],
                      status: Optional[str]) -> dict:
    if date_from and date_to and date_from > date_to:
        raise unprocessable("date_from must not be after date_to", ("query", "date_from"))
    query: dict = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status is not None:
        query["status"] = status
    return query


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    query = attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(query)
    # Sorting, skipping and limiting happen in MongoDB on an index, never in Python memory.
    cursor = (db.attendance_logs.find(query)
              .sort(ATTENDANCE_SORT)
              .skip((page - 1) * page_size)
              .limit(page_size))
    return page_out([record_out(doc) for doc in cursor], total, page, page_size)


TRACKED_FIELDS = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")


def stored_values(record: dict) -> dict:
    """The tracked fields of a stored record, with defaults for missing ones."""
    return {
        "status": record["status"],
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes") or 0,
        "overtime_minutes": record.get("overtime_minutes") or 0,
        "half_day": bool(record.get("half_day", False)),
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    day: Annotated[DateStr, Path(alias="date")],
    body: RegularizeRequest,
):
    emp = get_employee(emp_code)
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": day})
    if record is None:
        raise HTTPException(404, "no attendance record for that date")
    old = stored_values(record)

    # 1. Work out the final status and punch times (omitted fields keep the stored value).
    status = body.status or old["status"]
    if status in PRESENCE_STATUSES:
        punch_in = from_ms(body.punch_in) if body.punch_in is not None else old["punch_in"]
        punch_out = from_ms(body.punch_out) if body.punch_out is not None else old["punch_out"]
        if punch_in is None:
            raise unprocessable(f"status {status} requires a punch_in", ("body", "punch_in"))
        if body.punch_in is not None and attendance_date(punch_in, emp).isoformat() != day:
            raise unprocessable("punch_in must stay on the record's attendance date", ("body", "punch_in"))
        if punch_out is not None:
            check_punch_out(punch_in, punch_out, ("body", "punch_out"))
    else:
        if body.punch_in is not None or body.punch_out is not None:
            raise unprocessable(f"status {status} clears both punch times; do not send them", ("body",))
        punch_in = punch_out = None

    # 2. Recompute the derived fields from the final values (R2 - R5).
    new = {"status": status, "punch_in": punch_in, "punch_out": punch_out,
           **derive_fields(status, punch_in, punch_out, date.fromisoformat(day), emp)}

    # 3. Audit entry with only the fields that actually changed.
    changes = {field: {"from": old[field], "to": new[field]}
               for field in TRACKED_FIELDS if old[field] != new[field]}
    if not changes:
        raise unprocessable("the request changes nothing", ("body",))
    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # 4. Optimistic concurrency: write only if the record still looks exactly as we read it
    # (same punch fields, same number of history entries). A concurrent edit that got there
    # first makes this filter miss, so the loser gets 409 instead of overwriting the winner.
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "status": old["status"],
            "punch_in": old["punch_in"],
            "punch_out": old["punch_out"],
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, len(record.get("history") or [])]},
        },
        {"$set": new, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "the record was changed by another request; please retry")
    return record_out(updated)


# --------------------------------------------------------------------------- #
# 8. Analytics pipelines
#    Each endpoint's main aggregation is built by one function, so the explain endpoint
#    can run exactly the same pipeline.
# --------------------------------------------------------------------------- #
MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"
MAX_TREND_DAYS = 92


def month_bounds(month: str) -> tuple[date, date]:
    """'2026-07' -> (2026-07-01, 2026-07-31)."""
    first = date.fromisoformat(month + "-01")
    next_month = (first + timedelta(days=32)).replace(day=1)
    return first, next_month - timedelta(days=1)


def count_working_days(start: date, end: date) -> int:
    """R7: Mon-Fri days from start to end inclusive (0 when start is after end)."""
    days = (end - start).days + 1
    return sum(1 for i in range(max(days, 0)) if (start + timedelta(days=i)).weekday() < 5)


# Small builders for aggregation expressions. `prefix` is "" for a log document and "logs."
# when the log sits inside a joined employee document.
def is_presence(prefix: str = "") -> dict:
    return {"$in": [f"${prefix}status", PRESENCE_STATUSES]}  # R6


def is_weekday(prefix: str = "") -> dict:
    """The stored `date` string as a day of the week; ISO 1-5 is Mon-Fri (R7)."""
    day = {"$dateFromString": {"dateString": f"${prefix}date", "format": "%Y-%m-%d"}}
    return {"$lte": [{"$isoDayOfWeek": day}, 5]}


def is_late(prefix: str = "") -> dict:
    return {"$gt": [f"${prefix}late_minutes", 0]}


def present_value(prefix: str = "", weekdays_only: bool = True) -> dict:
    """R5 - R7: a presence record counts 1, or 0.5 when it is a half day (missing = false)."""
    counts = {"$and": [is_presence(prefix), is_weekday(prefix)]} if weekdays_only else is_presence(prefix)
    return {"$cond": [counts, {"$cond": [{"$ifNull": [f"${prefix}half_day", False]}, 0.5, 1]}, 0]}


def count_if(condition: dict) -> dict:
    return {"$sum": {"$cond": [condition, 1, 0]}}


def round_half_up_expr(expr, places: int) -> dict:
    """R8 inside MongoDB. $round rounds half to even, so do floor(x * 10^p + 0.5) / 10^p on
    Decimal128 (exact base-10 maths). The result stays Decimal128; null stays null."""
    scale = 10 ** places
    shifted = {"$add": [{"$multiply": [{"$toDecimal": expr}, scale]}, Decimal128("0.5")]}
    return {"$divide": [{"$floor": shifted}, scale]}


def employee_monthly_pipeline(emp_code: str, first: date, last: date) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": present_value()},
            "leave_days": count_if({"$eq": ["$status", "LEAVE"]}),
            "late_count": count_if(is_late()),          # weekends included (R7)
            "total_late_minutes": {"$sum": "$late_minutes"},  # $sum skips missing values
            "total_overtime_minutes": {"$sum": "$overtime_minutes"},
        }},
    ]


def department_summary_pipeline(first: date, last: date, department: Optional[str]) -> list:
    # R9: start from employees, so people with zero logs are part of the headcount, and
    # people who join after the month are filtered out before anything is joined.
    match = {"joined_on": {"$lte": last.isoformat()}}
    if department is not None:
        match["department"] = department
    has_hours = {"$and": [is_presence("logs."), {"$isNumber": "$logs.work_hours"}]}
    return [
        {"$match": match},
        {"$lookup": {  # per employee, served by the unique (emp_code, date) index
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
                {"$project": {"_id": 0, "status": 1, "date": 1, "work_hours": 1, "late_minutes": 1, "half_day": 1}},
            ],
            "as": "logs",
        }},
        # An employee with no logs keeps one row (without `logs`), so it still reaches $group.
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "employees": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": present_value("logs.")},
            # Average over records, not of per-employee averages: keep a sum and a count.
            "work_hours_sum": {"$sum": {"$cond": [has_hours, {"$toDecimal": "$logs.work_hours"}, 0]}},
            "work_hours_count": count_if(has_hours),
            "late_count": count_if(is_late("logs.")),
            "total_late_minutes": {"$sum": "$logs.late_minutes"},
            "leave_count": count_if({"$eq": ["$logs.status", "LEAVE"]}),
            "on_duty_count": count_if({"$eq": ["$logs.status", "ON_DUTY"]}),
        }},
        {"$sort": {"_id": 1}},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": {"$size": "$employees"},
            "present_days": 1,
            "avg_work_hours": {"$cond": [
                {"$gt": ["$work_hours_count", 0]},
                {"$toDouble": round_half_up_expr({"$divide": ["$work_hours_sum", "$work_hours_count"]}, 2)},
                None,
            ]},
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1,
        }},
    ]


def late_leaderboard_pipeline(first: date, last: date, limit: int, department: Optional[str]) -> list:
    pipeline = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "pipeline": [{"$project": {"_id": 0, "name": 1, "department": 1}}],
            "as": "employee",
        }},
        {"$unwind": "$employee"},  # drops logs whose employee does not exist
    ]
    if department is not None:  # rank inside the department only
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        # Standard competition ranking: ties share a rank and the next one is skipped (1, 2, 2, 4).
        # Sort only by the total, otherwise tied employees would get different ranks.
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # after ranking, so everyone tied at the cutoff stays
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": "$employee.name",
            "department": "$employee.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
    ]
    return pipeline


def department_trend_pipeline(members: list, first: date, last: date) -> list:
    """`members` are the department's employees ({emp_code, joined_on}), read beforehand."""
    start = datetime.combine(first, time(), tzinfo=UTC)
    end_exclusive = datetime.combine(last + timedelta(days=1), time(), tzinfo=UTC)
    joined_dates = [m["joined_on"] for m in members]
    return [
        {"$match": {"emp_code": {"$in": [m["emp_code"] for m in members]},
                    "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {
            "_id": "$date",
            "present_count": {"$sum": present_value(weekdays_only=False)},  # weekends count here
            "late_count": count_if(is_late()),
        }},
        {"$project": {"_id": 0, "day": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}},
                      "present_count": 1, "late_count": 1}},
        # Gap filling: one document for every calendar day; the upper bound is exclusive.
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [start, end_exclusive]}}},
        {"$set": {
            "date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}},
            "is_working_day": {"$lte": [{"$isoDayOfWeek": "$day"}, 5]},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
        }},
        # R9 for each day: department members who had joined by that day.
        {"$set": {"headcount": {"$size": {"$filter": {
            "input": {"$literal": joined_dates}, "as": "joined", "cond": {"$lte": ["$$joined", "$date"]},
        }}}}},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            round_half_up_expr({"$divide": [{"$toDecimal": "$present_count"}, "$headcount"]}, 4),
            None,
        ]}}},
        # Current row plus up to 6 preceding rows. Only rows inside the range exist, so the
        # first rows get a shorter window; $avg ignores the null rates.
        {"$setWindowFields": {
            "sortBy": {"day": 1},
            "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}},
        }},
        {"$sort": {"day": 1}},
        {"$project": {
            "_id": 0,
            "date": 1,
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": {"$toDouble": "$attendance_rate"},
            "moving_avg_7d": {"$toDouble": round_half_up_expr("$moving_avg_7d", 4)},
        }},
    ]


def trend_range(start: str, end: str) -> tuple[date, date]:
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if last < first:
        raise unprocessable("`to` must not be before `from`", ("query", "to"))
    if (last - first).days + 1 > MAX_TREND_DAYS:
        raise unprocessable(f"the range can not exceed {MAX_TREND_DAYS} days", ("query", "to"))
    return first, last


def department_members(department: str) -> list:
    return list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))


# --------------------------------------------------------------------------- #
# 9. Endpoints: analytics
# --------------------------------------------------------------------------- #
MonthParam = Annotated[str, Query(pattern=MONTH_PATTERN, description="Calendar month, YYYY-MM.")]
LimitParam = Annotated[int, Query(ge=1, le=50)]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthParam):
    emp = get_employee(emp_code)
    first, last = month_bounds(month)
    rows = list(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, first, last)))
    stats = rows[0] if rows else {}  # no logs in the month -> no group document

    # R7: count working days from joined_on for a mid-month joiner (0 if they join later).
    working_days = count_working_days(max(first, date.fromisoformat(emp["joined_on"])), last)
    present_days = stats.get("present_days", 0)
    attendance_pct = None
    if working_days:
        attendance_pct = float(round_half_up(Decimal(str(present_days)) * 100 / working_days, 2))
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": stats.get("leave_days", 0),
        "late_count": stats.get("late_count", 0),
        "total_late_minutes": stats.get("total_late_minutes", 0),
        "total_overtime_minutes": stats.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: MonthParam, department: Optional[str] = None):
    first, last = month_bounds(month)
    items = list(db.employees.aggregate(department_summary_pipeline(first, last, department)))
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: MonthParam, limit: LimitParam = 10, department: Optional[str] = None):
    first, last = month_bounds(month)
    items = list(db.attendance_logs.aggregate(late_leaderboard_pipeline(first, last, limit, department)))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    start: Annotated[DateStr, Query(alias="from")],
    end: Annotated[DateStr, Query(alias="to")],
):
    first, last = trend_range(start, end)
    members = department_members(department)
    if not members:
        raise HTTPException(404, "unknown department")
    items = list(db.attendance_logs.aggregate(department_trend_pipeline(members, first, last)))
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# 10. Admin: explain
# --------------------------------------------------------------------------- #
ExplainTarget = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard",
                        "department_trend"]


def require(**params) -> None:
    """422 for every required parameter that is missing for the explained endpoint."""
    missing = [name for name, value in params.items() if value is None]
    if missing:
        raise RequestValidationError([
            {"type": "missing", "loc": ("query", name), "msg": "Field required", "input": None} for name in missing
        ])


def aggregate_command(collection: str, pipeline: list) -> dict:
    return {"aggregate": collection, "pipeline": pipeline, "cursor": {}}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainTarget,
    emp_code: Optional[str] = None,
    month: Annotated[Optional[str], Query(pattern=MONTH_PATTERN)] = None,
    department: Optional[str] = None,
    limit: LimitParam = 10,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    start: Annotated[Optional[DateStr], Query(alias="from")] = None,
    end: Annotated[Optional[DateStr], Query(alias="to")] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    """Runs `explain` (executionStats) on the same query the endpoint runs, built by the same
    functions with the same parameters."""
    if endpoint == "attendance_list":
        collection = "attendance_logs"
        command = {
            "find": collection,
            "filter": attendance_filter(emp_code, date_from, date_to, status),
            "sort": dict(ATTENDANCE_SORT),
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
    elif endpoint == "employee_monthly":
        require(emp_code=emp_code, month=month)
        collection = "attendance_logs"
        command = aggregate_command(collection, employee_monthly_pipeline(emp_code, *month_bounds(month)))
    elif endpoint == "department_summary":
        require(month=month)
        collection = "employees"
        command = aggregate_command(collection, department_summary_pipeline(*month_bounds(month), department))
    elif endpoint == "late_leaderboard":
        require(month=month)
        collection = "attendance_logs"
        command = aggregate_command(collection, late_leaderboard_pipeline(*month_bounds(month), limit, department))
    else:  # department_trend
        require(department=department, **{"from": start, "to": end})
        first, last = trend_range(start, end)
        collection = "attendance_logs"
        pipeline = department_trend_pipeline(department_members(department), first, last)
        command = aggregate_command(collection, pipeline)

    explain = db.command("explain", command, verbosity="executionStats")
    # BSON types (Timestamp, Decimal128, datetimes) -> plain JSON
    return {"endpoint": endpoint, "collection": collection,
            "explain": json.loads(json_util.dumps(explain, json_options=json_util.RELAXED_JSON_OPTIONS))}
