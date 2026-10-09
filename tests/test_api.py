"""
Tests for app/main.py. They use their own database (attendance_test), never your real data.

    pip install -r requirements-dev.txt
    pytest -q
"""
import os

os.environ["MONGO_DB"] = "attendance_test"  # must be set before the app is imported

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app, db


def ms(ist: str) -> int:
    """'2026-07-06T09:28:00' as IST wall-clock time -> epoch milliseconds."""
    return int(datetime.fromisoformat(ist + "+05:30").timestamp() * 1000)


@pytest.fixture()
def client():
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    with TestClient(app) as test_client:
        yield test_client


def add_employee(client, code, department="Engineering", joined_on="2026-01-05", **extra):
    body = {"emp_code": code, "name": f"Person {code}", "email": f"{code.lower()}@example.com",
            "department": department, "joined_on": joined_on, **extra}
    response = client.post("/employees", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def punch_in(client, code, ist, status="PRESENT"):
    return client.post("/attendance/punch-in", json={"emp_code": code, "punched_at": ms(ist), "status": status})


def punch_out(client, code, ist):
    return client.post("/attendance/punch-out", json={"emp_code": code, "punched_at": ms(ist)})


# --------------------------------------------------------------------------- #
# Employees
# --------------------------------------------------------------------------- #
def test_employee_create_validation_and_duplicate(client):
    created = add_employee(client, "EMP0001")
    assert created["shift_start"] == "09:30" and isinstance(created["created_at"], int)
    assert "_id" not in created
    assert client.post("/employees", json={**created, "name": "Again"}).status_code == 409
    for bad in ({"emp_code": "E1"}, {"shift_end": "09:30"}, {"joined_on": "2026-02-30"}, {"email": "nope"}):
        body = {"emp_code": "EMP0002", "name": "B", "email": "b@x.com", "department": "Eng",
                "joined_on": "2026-01-05", **bad}
        assert client.post("/employees", json=body).status_code == 422, bad


def test_employee_list_pagination_filter_and_sort(client):
    for code, dept in [("EMP0003", "Sales"), ("EMP0001", "Engineering"), ("EMP0002", "Sales")]:
        add_employee(client, code, department=dept)
    page = client.get("/employees?page=1&page_size=2").json()
    assert [e["emp_code"] for e in page["items"]] == ["EMP0001", "EMP0002"] and page["total"] == 3
    sales = client.get("/employees?department=Sales").json()
    assert sales["total"] == 2 and [e["emp_code"] for e in sales["items"]] == ["EMP0002", "EMP0003"]
    assert client.get("/employees?page=0").status_code == 422
    assert client.get("/employees?page_size=101").status_code == 422


def test_concurrent_duplicate_creates_give_one_201(client):
    body = {"emp_code": "EMP0001", "name": "A", "email": "a@x.com", "department": "Eng", "joined_on": "2026-01-05"}
    with ThreadPoolExecutor(10) as pool:
        codes = list(pool.map(lambda _: client.post("/employees", json=body).status_code, range(10)))
    assert sorted(codes) == [201] + [409] * 9


# --------------------------------------------------------------------------- #
# Punch-in: R1, R2, validation, race
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("ist_time, expected_late", [
    ("09:40:00", 0), ("09:40:01", 10), ("10:15:59", 45), ("09:15:00", 0),
])
def test_late_minutes_r2(client, ist_time, expected_late):
    add_employee(client, "EMP0001")
    response = punch_in(client, "EMP0001", f"2026-07-06T{ist_time}")
    assert response.status_code == 201
    assert response.json()["late_minutes"] == expected_late


def test_instants_are_truncated_to_seconds_r1(client):
    add_employee(client, "EMP0001")
    punched_at = ms("2026-07-06T09:40:00") + 900  # 09:40:00.900 counts as 09:40:00
    body = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": punched_at}).json()
    assert body["late_minutes"] == 0 and body["punch_in"] == punched_at - 900


def test_overnight_punch_in_belongs_to_previous_day_r1(client):
    add_employee(client, "EMP0005", shift_start="22:00", shift_end="06:00")
    early = punch_in(client, "EMP0005", "2026-07-07T02:00:00").json()
    assert early["date"] == "2026-07-06" and early["late_minutes"] == 240
    evening = punch_in(client, "EMP0005", "2026-07-07T21:55:00").json()
    assert evening["date"] == "2026-07-07" and evening["late_minutes"] == 0


@pytest.mark.parametrize("bad", [1783312500, 1783312500000.0, "1783312500000", True, 5_000_000_000_000])
def test_bad_timestamps_are_422(client, bad):
    add_employee(client, "EMP0001")
    response = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": bad})
    assert response.status_code == 422


def test_punch_in_errors(client):
    add_employee(client, "EMP0001")
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP9999"}).status_code == 404
    assert punch_in(client, "EMP0001", "2026-07-06T09:00:00", status="ABSENT").status_code == 422
    assert punch_in(client, "EMP0001", "2026-07-06T09:00:00").status_code == 201
    assert punch_in(client, "EMP0001", "2026-07-06T11:00:00").status_code == 409


def test_concurrent_punch_ins_give_one_201(client):
    add_employee(client, "EMP0001")
    with ThreadPoolExecutor(10) as pool:
        codes = list(pool.map(lambda _: punch_in(client, "EMP0001", "2026-07-06T09:00:00").status_code, range(10)))
    assert sorted(codes) == [201] + [409] * 9


# --------------------------------------------------------------------------- #
# Punch-out: R3, R4, R5, errors, race (values from DATA_MODEL.md samples)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("shift, punch_in_at, punch_out_at, expected", [
    (("09:30", "18:30"), "2026-07-06T09:28", "2026-07-06T18:35", (9.12, 0, False)),   # sample 1
    (("09:30", "18:30"), "2026-07-06T10:05", "2026-07-06T19:10", (9.08, 40, False)),  # sample 2
    (("09:30", "18:30"), "2026-07-07T09:30", "2026-07-07T13:00", (3.5, 0, True)),     # sample 4
    (("22:00", "06:00"), "2026-07-06T21:55", "2026-07-07T06:40", (8.75, 40, False)),  # sample 7
    (("09:30", "18:30"), "2026-07-06T10:00", "2026-07-06T18:02:42", (8.05, 0, False)),  # 8.045 -> half-up
    (("09:30", "18:30"), "2026-07-06T09:30", "2026-07-06T13:59:59", (4.5, 0, False)),  # 4.4997 rounds to 4.50
])
def test_punch_out_derived_fields(client, shift, punch_in_at, punch_out_at, expected):
    add_employee(client, "EMP0001", shift_start=shift[0], shift_end=shift[1])
    punch_in(client, "EMP0001", punch_in_at)
    body = punch_out(client, "EMP0001", punch_out_at).json()
    assert (body["work_hours"], body["overtime_minutes"], body["half_day"]) == expected
    assert body["history"] == []  # punches never write history


def test_punch_out_errors(client):
    add_employee(client, "EMP0001")
    assert punch_out(client, "EMP9999", "2026-07-06T18:00").status_code == 404
    assert punch_out(client, "EMP0001", "2026-07-06T18:00").status_code == 404  # no punch-in yet
    punch_in(client, "EMP0001", "2026-07-06T09:30")
    assert punch_out(client, "EMP0001", "2026-07-06T09:30").status_code == 422  # not after punch_in
    assert punch_out(client, "EMP0001", "2026-07-07T09:31").status_code == 422  # more than 24 h
    assert punch_out(client, "EMP0001", "2026-07-06T18:30").status_code == 200
    assert punch_out(client, "EMP0001", "2026-07-06T19:00").status_code == 409  # already out


def test_concurrent_punch_outs_give_one_200(client):
    add_employee(client, "EMP0001")
    punch_in(client, "EMP0001", "2026-07-06T09:30")
    with ThreadPoolExecutor(10) as pool:
        codes = list(pool.map(lambda _: punch_out(client, "EMP0001", "2026-07-06T18:30").status_code, range(10)))
    assert sorted(codes) == [200] + [409] * 9


# --------------------------------------------------------------------------- #
# PATCH (regularization) and history
# --------------------------------------------------------------------------- #
def regularize(client, code, day, **changes):
    body = {"reason": "biometric glitch", "regularized_by": "hr.admin", **changes}
    return client.patch(f"/attendance/{code}/{day}", json=body)


def test_regularize_sample_8(client):
    add_employee(client, "EMP0001")
    punch_in(client, "EMP0001", "2026-07-07T10:05")
    punch_out(client, "EMP0001", "2026-07-07T18:30")
    response = regularize(client, "EMP0001", "2026-07-07", punch_in=ms("2026-07-07T09:28"), work_hours=99)
    assert response.status_code == 200
    body = response.json()
    assert (body["work_hours"], body["late_minutes"]) == (9.03, 0)  # recomputed; client value ignored
    [entry] = body["history"]
    assert entry["by"] == "hr.admin" and isinstance(entry["at"], int)
    assert entry["changes"] == {
        "punch_in": {"from": ms("2026-07-07T10:05"), "to": ms("2026-07-07T09:28")},
        "late_minutes": {"from": 35, "to": 0},
        "work_hours": {"from": 8.42, "to": 9.03},
    }


def test_regularize_rules(client):
    add_employee(client, "EMP0001")
    punch_in(client, "EMP0001", "2026-07-06T09:30")
    day = "2026-07-06"
    assert regularize(client, "EMP0001", day, status="ABSENT", punch_in=ms(f"{day}T09:00")).status_code == 422
    assert regularize(client, "EMP0001", day).status_code == 422                     # changes nothing
    assert regularize(client, "EMP0001", day, status="PRESENT").status_code == 422   # same value
    assert regularize(client, "EMP0001", day, punch_in=ms("2026-07-07T09:00")).status_code == 422  # other day
    assert regularize(client, "EMP0001", day, punch_out=ms(f"{day}T09:00")).status_code == 422     # before in
    assert regularize(client, "EMP9999", day, status="WFH").status_code == 404
    assert regularize(client, "EMP0001", "2026-07-20", status="WFH").status_code == 404

    leave = regularize(client, "EMP0001", day, status="LEAVE").json()
    assert (leave["punch_in"], leave["punch_out"], leave["late_minutes"]) == (None, None, 0)
    assert regularize(client, "EMP0001", day, status="PRESENT").status_code == 422   # needs a punch_in
    back = regularize(client, "EMP0001", day, status="WFH", punch_in=ms(f"{day}T09:45")).json()
    assert back["late_minutes"] == 15 and len(back["history"]) == 2


def test_concurrent_regularizations_keep_every_history_entry(client):
    add_employee(client, "EMP0001")
    punch_in(client, "EMP0001", "2026-07-06T09:30")
    punch_out(client, "EMP0001", "2026-07-06T18:30")

    def edit(minute):
        return regularize(client, "EMP0001", "2026-07-06", punch_out=ms(f"2026-07-06T19:{minute:02d}")).status_code

    with ThreadPoolExecutor(8) as pool:
        codes = list(pool.map(edit, range(8)))
    record = client.get("/attendance?emp_code=EMP0001").json()["items"][0]
    assert set(codes) <= {200, 409}
    assert len(record["history"]) == codes.count(200)  # nothing silently lost


# --------------------------------------------------------------------------- #
# Attendance list
# --------------------------------------------------------------------------- #
def test_attendance_list_sort_filters_and_validation(client):
    for code in ("EMP0002", "EMP0001"):
        add_employee(client, code)
        punch_in(client, code, "2026-07-06T09:30")
        punch_in(client, code, "2026-07-07T09:30", status="WFH")
    items = client.get("/attendance").json()["items"]
    assert [(i["date"], i["emp_code"]) for i in items] == [
        ("2026-07-07", "EMP0001"), ("2026-07-07", "EMP0002"), ("2026-07-06", "EMP0001"), ("2026-07-06", "EMP0002")]
    wfh = client.get("/attendance?status=WFH&page_size=1").json()
    assert wfh["total"] == 2 and len(wfh["items"]) == 1
    assert client.get("/attendance?date_from=2026-07-07&date_to=2026-07-06").status_code == 422
    assert client.get("/attendance?status=FOO").status_code == 422


# --------------------------------------------------------------------------- #
# Analytics. Seeded straight into MongoDB, the way the grader does it.
# July 2026: 1 July is a Wednesday, 4-5 and 11-12 are weekends, 23 working days.
# --------------------------------------------------------------------------- #
def log(code, day, status="PRESENT", work_hours=8.0, late=0, overtime=0, half_day=False):
    presence = status in ("PRESENT", "WFH", "ON_DUTY")
    return {"emp_code": code, "date": day, "status": status,
            "punch_in": datetime.fromisoformat(day + "T04:00:00+00:00") if presence else None,
            "punch_out": None, "work_hours": work_hours if presence else None,
            "late_minutes": late, "overtime_minutes": overtime, "half_day": half_day, "history": []}


@pytest.fixture()
def july(client):
    db.employees.insert_many([
        {"emp_code": code, "name": f"Person {code}", "email": "x@example.com", "department": dept,
         "shift_start": "09:30", "shift_end": "18:30", "joined_on": joined,
         "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc)}
        for code, dept, joined in [
            ("EMP0001", "Engineering", "2026-01-05"),
            ("EMP0002", "Engineering", "2026-07-13"),  # joins mid-month
            ("EMP0003", "Engineering", "2026-01-05"),  # no logs at all
            ("EMP0004", "Sales", "2026-01-05"),
            ("EMP0005", "Sales", "2026-08-03"),        # joins after July
            ("EMP0006", "Sales", "2026-01-05"),
        ]
    ])
    legacy = log("EMP0002", "2026-07-14", late=35)
    del legacy["half_day"], legacy["history"]
    db.attendance_logs.insert_many([
        log("EMP0001", "2026-07-04", work_hours=5.0, late=15),   # Saturday
        log("EMP0001", "2026-07-06", work_hours=9.0, overtime=40),
        log("EMP0001", "2026-07-07", status="WFH", work_hours=4.0, late=20, half_day=True),
        log("EMP0001", "2026-07-08", status="LEAVE"),
        legacy,
        log("EMP0004", "2026-07-06", work_hours=7.25, late=20),
        log("EMP0004", "2026-07-07", status="ON_DUTY", work_hours=8.0),
        log("EMP0006", "2026-07-06", work_hours=9.5, late=50),
        log("EMP9999", "2026-07-06", late=100),                 # employee does not exist
        log("EMP0001", "2026-06-30", late=99),                  # other month
    ])
    return client


def test_employee_monthly(july):
    one = july.get("/analytics/employees/EMP0001/monthly?month=2026-07").json()
    assert one == {"emp_code": "EMP0001", "month": "2026-07", "working_days": 23, "present_days": 1.5,
                   "leave_days": 1, "late_count": 2, "total_late_minutes": 35, "total_overtime_minutes": 40,
                   "attendance_pct": 6.52}
    joiner = july.get("/analytics/employees/EMP0002/monthly?month=2026-07").json()
    assert (joiner["working_days"], joiner["present_days"], joiner["attendance_pct"]) == (15, 1, 6.67)
    assert july.get("/analytics/employees/EMP0003/monthly?month=2026-07").json()["attendance_pct"] == 0.0
    assert july.get("/analytics/employees/EMP0005/monthly?month=2026-07").json()["attendance_pct"] is None
    assert july.get("/analytics/employees/EMP9999/monthly?month=2026-07").status_code == 404
    assert july.get("/analytics/employees/EMP0001/monthly?month=2026-13").status_code == 422


def test_department_summary(july):
    items = july.get("/analytics/departments/summary?month=2026-07").json()["items"]
    assert items == [
        {"department": "Engineering", "headcount": 3, "present_days": 2.5, "avg_work_hours": 6.5,
         "late_count": 3, "total_late_minutes": 70, "leave_count": 1, "on_duty_count": 0},
        {"department": "Sales", "headcount": 2, "present_days": 3, "avg_work_hours": 8.25,
         "late_count": 2, "total_late_minutes": 70, "leave_count": 0, "on_duty_count": 1},
    ]
    only_sales = july.get("/analytics/departments/summary?month=2026-07&department=Sales").json()["items"]
    assert [i["department"] for i in only_sales] == ["Sales"]


def test_late_leaderboard_ties(july):
    def board(query):
        return [(i["rank"], i["emp_code"]) for i in july.get(f"/analytics/leaderboard/late?month=2026-07{query}").json()["items"]]

    # totals: EMP0006 50, EMP0001 35, EMP0002 35, EMP0004 20 -> ranks 1, 2, 2, 4
    assert board("") == [(1, "EMP0006"), (2, "EMP0001"), (2, "EMP0002"), (4, "EMP0004")]
    assert board("&limit=2") == [(1, "EMP0006"), (2, "EMP0001"), (2, "EMP0002")]  # tie at the cutoff kept
    assert board("&limit=3") == board("&limit=2")                                  # no rank 3 exists
    assert board("&department=Engineering") == [(1, "EMP0001"), (1, "EMP0002")]
    assert july.get("/analytics/leaderboard/late?month=2026-07&limit=51").status_code == 422


def test_department_trend(july):
    response = july.get("/analytics/departments/Engineering/trend?from=2026-07-03&to=2026-07-14")
    rows = {row["date"]: row for row in response.json()["items"]}
    assert len(rows) == 12  # one row per calendar day, weekends and empty days included
    assert rows["2026-07-03"] == {"date": "2026-07-03", "is_working_day": True, "headcount": 2, "present_count": 0,
                                  "late_count": 0, "attendance_rate": 0.0, "moving_avg_7d": 0.0}
    assert (rows["2026-07-04"]["present_count"], rows["2026-07-04"]["attendance_rate"]) == (1, None)  # weekend
    assert rows["2026-07-07"]["attendance_rate"] == 0.25        # half day counts 0.5 of 2 people
    assert rows["2026-07-08"]["moving_avg_7d"] == 0.1875        # mean of 0, 0.5, 0.25, 0
    assert rows["2026-07-14"]["headcount"] == 3                 # mid-month joiner counted from 13 July
    assert (rows["2026-07-14"]["attendance_rate"], rows["2026-07-14"]["moving_avg_7d"]) == (0.3333, 0.0667)
    assert july.get("/analytics/departments/Nope/trend?from=2026-07-01&to=2026-07-02").status_code == 404
    assert july.get("/analytics/departments/Engineering/trend?from=2026-07-02&to=2026-07-01").status_code == 422
    assert july.get("/analytics/departments/Engineering/trend?from=2026-01-01&to=2026-04-03").status_code == 422


# --------------------------------------------------------------------------- #
# Explain
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("endpoint, query", [
    ("attendance_list", ""),
    ("employee_monthly", "?emp_code=EMP0001&month=2026-07"),
    ("department_summary", "?month=2026-07"),
    ("late_leaderboard", "?month=2026-07"),
    ("department_trend", "?department=Engineering&from=2026-07-01&to=2026-07-31"),
])
def test_explain_uses_an_index(july, endpoint, query):
    response = july.get(f"/admin/explain/{endpoint}{query}")
    assert response.status_code == 200
    assert "IXSCAN" in response.text and "COLLSCAN" not in response.text


def test_explain_missing_parameter_is_422(july):
    assert july.get("/admin/explain/employee_monthly?month=2026-07").status_code == 422
