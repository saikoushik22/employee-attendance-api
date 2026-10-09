# DECISIONS.md

1. **Indexes.**
   - `attendance_logs (emp_code, date)`, unique: enforces one record per day and serves every per-employee date
     range (monthly, punch-out, PATCH, the summary's `$lookup`, the trend).
   - `attendance_logs (date desc, emp_code)`: the list sort and the leaderboard's month scan.
   - `employees.emp_code`, unique.
   - `employees (department, emp_code)`.
   - `employees.joined_on`: headcount.

   Rejected: an index on `status`. Five values is too few to be selective.

2. **Punch-in race.** Both requests compute the same `(emp_code, date)` and call `insert_one` without reading
   first. The unique index lets exactly one insert win. The other gets `DuplicateKeyError`, which I return as 409.

3. **Ties.** `$rank` sorted only by total gives tied employees the same rank and skips the next one. I filter
   `rank <= limit` after ranking, so everyone tied at the cutoff is returned, even beyond `limit`. Ties are ordered
   by `emp_code`.

4. **Headcount.** The pipeline starts from `employees` (`joined_on <= month end`) and looks up their logs.
   `$unwind` with `preserveNullAndEmptyArrays` keeps people without logs, and I count distinct `emp_code`s.

5. **100x data.** I would keep one pre-aggregated document per department per day, updated on punch and PATCH,
   so the summary and trend read a few rows instead of joining every log.
