# Employee Attendance & Analytics API

FastAPI + MongoDB service for punch-in/out, manual corrections with an audit trail, and analytics built on
MongoDB aggregation pipelines. All application code is in `app/main.py`.

## Run

Requires Python 3.11+ and MongoDB 6.0+ (tested on MongoDB 7.0.14, Python 3.13).

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
# set MONGO_URI and MONGO_DB in the environment (or in a local .env file)
uvicorn app.main:app --port 8000
```

`MONGO_URI` / `MONGO_DB` default to `mongodb://localhost:27017` / `attendance_db`. Real environment variables take
priority over `.env`. Indexes are created at startup (idempotent), so no manual step is needed. Interactive docs are
at `http://localhost:8000/`.

Optional: `python sample_seed.py` loads the nine sample documents.

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q
```

The tests use a separate database (`attendance_test`). They cover R1-R5 with the examples from the spec, the
validation and error codes, the three race conditions, and the analytics with awkward data (ties, weekend records,
mid-month joiners, employees without logs, legacy records).

## Notes

- Everything in the contract is implemented. Nothing is missing.
- `REVIEW.md` lists the defects found in the starter, and `DECISIONS.md` answers the five questions.
- Interpretations where the contract leaves room:
  - The trend's 92-day limit counts days inclusively, so a full quarter is allowed.
  - Explicit `null` for `status`/`punch_in`/`punch_out` in a PATCH is rejected with 422, because the fields are not
    nullable. Omit a field to keep its value.
  - Punch-out does not touch `late_minutes`, which is set at punch-in.
  - `avg_work_hours` includes weekend presence records. Only `present_days` is weekday-only (R7).
