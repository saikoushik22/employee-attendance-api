# REVIEW.md

Review of the starter `app/main.py`. Line numbers refer to the **original starter file**. The examples use shift
09:30 - 18:30 unless stated otherwise.

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `MongoClient(...)`, line 22 | Client is not `tz_aware`, so PyMongo returns **naive** datetimes. Any maths or `.timestamp()` on them silently uses the server's local time zone. | Read a seeded record on an IST laptop and convert `punch_in` with `.timestamp()`: the value is 5 h 30 min off. Subtracting it from an aware datetime raises `TypeError` (500). | `MongoClient(..., tz_aware=True)`; every datetime in the code is UTC-aware. Conversion to epoch ms uses integer maths in `to_ms()`. |
| 2 | Whole file (no start-up hook) | No indexes are created. There is no unique index on `employees.emp_code` or `attendance_logs (emp_code, date)`. Every list and analytics query is a collection scan. | `explain` on `GET /attendance` shows `COLLSCAN`; the race tests in #9 and #14 create duplicates. | `create_indexes()` runs in the FastAPI `lifespan` (idempotent). See DECISIONS.md. |
| 3 | `compute_late_minutes`, line 35 | `punch_in.replace(hour=h, minute=m)` builds the shift start in whatever zone `punch_in` happens to be in, not IST. It also uses the punch's calendar day, so an overnight shift is compared with the wrong day. | On a UTC server, a punch at 10:05 IST is the naive 04:35, so it gets `late_minutes = 0` (expected 35). For shift 22:00-06:00, a punch at 02:00 IST gets 0 (expected 240). | Shift start is built from the record's attendance date in IST (`shift_bounds()`), and the punch is an aware UTC instant. |
| 4 | `compute_late_minutes`, line 37 | Grace check `minutes > 10` compares **truncated minutes**, not time. Anything from 10:00 to 10:59 after shift start counts as on time. | Punch at 09:40:30 → starter returns 0; R2 says 09:40:01 is already late, so the answer is 10. | Compare whole **seconds**: `seconds > 600`, then `seconds // 60`. Punches are truncated to whole seconds first (R1). |
| 5 | `compute_work_hours`, line 41 | `round(x, 2)` is float/banker's rounding, not half-up (R4). | 8 h 2 min 42 s = 8.045 h → `round()` gives **8.04**, expected **8.05**. 600 such durations exist in a 12-hour day. | `Decimal(seconds) / 3600` quantized with `ROUND_HALF_UP` (`round_half_up()`). |
| 6 | `compute_overtime`, line 47 | No 30-minute minimum (R3). | DATA_MODEL sample 1 (out 18:35) → starter 5, expected 0. | `minutes if minutes >= 30 else 0`. |
| 7 | `compute_overtime`, line 46 | Shift end always uses the attendance date. For an overnight shift the end is on the **next** day. | DATA_MODEL sample 7 (22:00-06:00, out 06:40 next day) → starter **1480**, expected 40. | `shift_bounds()` moves the end to the next day when `shift_end <= shift_start`. |
| 8 | `compute_overtime`, line 47 | `punch_out - end` mixes a naive `punch_out` (see #1, #12) with an aware `end`. | `TypeError: can't subtract offset-naive and offset-aware datetimes` → 500. | All datetimes are aware (#1, #12). |
| 9 | `create_employee`, lines 79-83 | Check-then-insert. Two concurrent creates both pass `find_one` and both insert. | Send 10 parallel `POST /employees` with the same `emp_code` → several 201s and duplicate employees. | Unique index on `emp_code`; just `insert_one` and turn `DuplicateKeyError` into 409. |
| 10 | `EmployeeIn`, lines 53-60 | No validation: `emp_code` pattern, email format, lengths, `HH:MM` shift times, real `joined_on` date, `shift_start != shift_end`. | `{"emp_code": "x", "joined_on": "05/01/2026", "shift_start": "25:99"}` → 201; analytics then compare garbage strings. | Pydantic constraints from the contract, a date validator, and a model validator for equal shift times → 422. |
| 11 | `create_employee`, lines 82, 85 | `created_at = datetime.now()` is naive local time. PyMongo stores it as if it were UTC, so on an IST machine it is 5 h 30 min in the future. It is also returned as an ISO string, not epoch ms. | Create an employee at 10:00 IST and read `created_at` back from MongoDB → 10:00 UTC. The response has `"2026-..T..."` instead of an integer. | `datetime.now(UTC)` (seconds precision); responses go through `employee_out()`, which returns epoch ms. |
| 12 | `punch_in`, line 102 | `datetime.fromtimestamp(ms / 1000)` gives the **server's local** naive time. The date is taken in the server zone (not IST), and the stored instant shifts by the server's UTC offset. Milliseconds are not truncated (R1). | On a UTC server, a 02:00 IST punch on 7 July is dated `2026-07-06`. On an IST server, the stored BSON instant is 5 h 30 min late. | `from_ms()` → aware UTC instant truncated to seconds; `attendance_date()` converts to IST. |
| 13 | `punch_in`, line 103 | The R1 overnight rule is missing. | Shift 22:00-06:00, punch 02:00 IST on the 7th → starter `date = 2026-07-07`, expected `2026-07-06`. | `attendance_date()` moves a punch earlier than `shift_end` to the previous day. |
| 14 | `punch_in`, lines 104-118 | Check-then-insert race: two simultaneous punch-ins both see no record and both insert. | 10 parallel identical punch-ins → several 201s and duplicate (`emp_code`, `date`) records. | Unique index on (`emp_code`, `date`); `insert_one` and turn `DuplicateKeyError` into 409. Verified: 1 × 201 and 9 × 409. |
| 15 | `punch_in`, lines 101, 113 | Unknown employee: `emp` is `None` and `emp["shift_start"]` raises `TypeError`. | `POST /attendance/punch-in {"emp_code": "EMP9999"}` → **500**, expected 404. | `get_employee()` raises 404 before anything else. |
| 16 | `PunchInIn`, lines 63-66 | `punched_at: int` (lax) accepts strings, whole floats and seconds; `status: str` accepts anything, including `ABSENT`. | `"punched_at": "1783312500000"`, `1783312500000.0` or `1783312500` (seconds, lands in 1970) → 201. `"status": "ABSENT"` or `"FOO"` → 201. | `EpochMs = StrictInt` in the range 1e11 to 4102444800000; `status: Literal["PRESENT","WFH","ON_DUTY"]` → 422. |
| 17 | `punch_in` line 119, `list_attendance` line 149 | Responses contain `id` (the Mongo `_id`), and datetimes are serialised as ISO strings, not epoch ms. | The response has `"id": "66f…"` and `"punch_in": "2026-07-06T09:28:00"`; the contract forbids `_id` and requires integer ms. | `record_out()` builds the contract shape: no id, instants as ms. |
| 18 | `list_employees`, line 93 | `skip = page * page_size`: page 1 skips the first page. | 25 employees, `?page=1` → items 21-25 instead of 1-20. | `skip((page - 1) * page_size)`. |
| 19 | `list_employees`, line 94 | `total` counts the whole collection, ignoring the `department` filter (R10). | `?department=Sales` with 2 sales people out of 6 → `total: 6`. | `count_documents(query)` with the same filter. |
| 20 | `list_employees`, line 95 | No sort, so order and paging are not deterministic. The contract says sorted by `emp_code`. | Insert EMP0002 before EMP0001 → the list comes back in insertion order. | `.sort("emp_code", 1)` (served by the unique index). |
| 21 | `list_employees` / `list_attendance` params, lines 89, 130-131 | No bounds on `page` / `page_size` (R10). | `/employees?page=-1` → `skip(-20)` raises → **500**. `page_size=0` → `limit(0)` returns **everything**. `page_size=100000` is accepted. | `Query(ge=1)` and `Query(ge=1, le=100)` → 422. |
| 22 | `list_attendance`, lines 144-147 | Loads every matching document into Python, then sorts and slices in memory. | With about 100k records one request reads the whole collection: slow, high memory, and `COLLSCAN` in `explain`. | `find(query).sort(...).skip(...).limit(...)` in MongoDB on the `(date desc, emp_code)` index; `count_documents(query)` for `total`. |
| 23 | `list_attendance`, line 145 | Sort is by `date` only. The contract says `date` desc, **then `emp_code` asc**, so records on the same date come back in random order. | Two employees on the same day appear in insertion order, and pages can overlap. | `sort([("date", -1), ("emp_code", 1)])`. |
| 24 | `list_attendance`, lines 126-141 | `date_from` / `date_to` / `status` are not validated, and `date_from > date_to` is not rejected. | `?date_from=2026-13-45` or `?status=FOO` → 200 with nothing found. `date_from=2026-07-08&date_to=2026-07-01` → 200 instead of 422. | `DateStr` validator (pattern plus real date), `Status` literal, and an explicit 422 when `date_from > date_to`. |
| 25 | `list_attendance` / responses | Legacy records without `half_day` / `history` (DATA_MODEL sample 9) are returned with those fields missing. | `GET /attendance?emp_code=EMP0002` → no `half_day` or `history` keys; the contract marks both as required. | `record_out()` fills defaults: `false`, `[]`, `0`. |
| 26 | `health`, lines 72-74 | Always returns 200, even when MongoDB is down. The contract says 200 only if MongoDB answers a ping, else 503. | Stop MongoDB → `/health` still returns `{"status": "ok"}`. | `client.admin.command("ping")` → 503 on `PyMongoError`. Short `serverSelectionTimeoutMS`, so it does not hang. |

## Looked at and decided were **not** defects

- **`load_dotenv()`** (line 20): by default it does not override variables that already exist, so real environment
  variables win, as required. Kept.
- **`IST = timezone(timedelta(hours=5, minutes=30))`** (line 25): correct. India has no daylight saving, so a fixed
  offset is right.
- **`(page - 1) * page_size` in `list_attendance`** (line 147): this slice was correct. Only the employee list had the
  off-by-one. The logic moved into MongoDB anyway (#22).
- **`doc.pop("_id", None)` after `insert_one`** (line 84): needed, because `insert_one` adds `_id` to the dict it is
  given. The new serialisers achieve the same thing by whitelisting fields.
- **`int(... / 60)` in the helpers**: for positive durations `int()` truncation equals floor, which is what R2/R3 ask
  for. Negative values are discarded by the rules anyway.
- **The work-hours formula** `(out - in) / 3600`: correct. Only its rounding was wrong (#5).
- **09:40:00.900 in the starter's late check**: it happens to give 0 (correct), but only because of the minutes bug
  (#4). Once seconds are compared, the value must be truncated to whole seconds first (R1), so `from_ms()` truncates.
- **Defaults** `shift_start = "09:30"`, `shift_end = "18:30"`, `status = "PRESENT"`, and status code 201 on create /
  punch-in: they match the contract.
- **Reusing the helpers**: I kept their names and purpose, but changed their signatures. They now receive the shift
  start/end as aware instants already resolved for the attendance date, which removes the time-zone and overnight
  bugs in one place.
