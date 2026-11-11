"""Read-only SQL for the Student AI Agent.

The AI writes a SELECT against two safe virtual tables; this module wraps it so it can only ever see
the active import (and the dashboard filters), never other tables, and can never change anything.

    students(id, name, mobile, alt_mobile, email, batch, state, college, college_group, mbbs_batch,
             company, payment_mode, status, employee, total_fee, discount, paid, due,
             joining_date, joining_month, due_date, events_attended, attended_events)
    student_events(student_id, name, batch, college, college_group, state, joining_date, event)

college_group = one clean name per real college (spelling variants like "GMC Kadapa" /
"Government Medical College, Kadapa" share the same college_group).
"""
import re
import time

import student_data

MAX_ROWS = 500
TIMEOUT_MS = 8000

FORBIDDEN = re.compile(
    r"\b(insert|update|delete|merge|drop|alter|create|grant|revoke|truncate|copy|call|do|execute|prepare|deallocate|"
    r"listen|notify|unlisten|vacuum|analyze|cluster|reindex|refresh|set|reset|lock|comment|security|discard|load|"
    r"import|into|returning|pg_\w+|current_setting|set_config|dblink\w*|lo_\w+|txid_\w+|query_to_xml|xmlparse|"
    r"student_records|student_imports|information_schema|pg_catalog|question_\w+|attendance_\w+)\b", re.I)

SCHEMA_DOC = """Tables you can query (PostgreSQL syntax, read-only, already limited to the active import and dashboard filters):

students — one row per student
  id, name, mobile, alt_mobile, email, batch, state (state of the medical college), college (exactly as typed in Excel),
  college_group (ONE clean name per real college — spelling variants merged; prefer this for counting/grouping by college),
  mbbs_batch, company, payment_mode, status, employee (counsellor who enrolled them),
  total_fee, discount, paid, due (numeric INR), joining_date (date; admission date), joining_month (text 'YYYY-MM'),
  due_date (date), events_attended (int), attended_events (jsonb array of class/event names)

student_events — one row per (student, attended class/event)
  student_id, name, batch, college, college_group, state, joining_date, event

Tips: use ILIKE '%term%' for fuzzy text; to_char(joining_date,'Mon YYYY') for month labels;
date_trunc('month', joining_date) for monthly grouping; COUNT(DISTINCT student_id) on student_events."""


def _college_groups(cursor, where, params):
    """Map every stored college spelling to one clean name, using the same grouping as the question parser."""
    import student_nlu
    cursor.execute(f"SELECT college, COUNT(*) FROM student_records {where} AND college IS NOT NULL AND college <> '' GROUP BY 1", params)
    pairs = []
    for e in student_nlu.build_entities("college", cursor.fetchall()):
        label = e.label
        pairs += [(v, label) for v in e.variants]
    return pairs


def validate(sql):
    s = (sql or "").strip().rstrip(";").strip()
    if not s:
        raise ValueError("Empty query")
    if len(s) > 6000:
        raise ValueError("Query too long")
    if ";" in s:
        raise ValueError("Only one statement is allowed")
    if "--" in s or "/*" in s:
        raise ValueError("Comments are not allowed in queries")
    if not re.match(r"^(select|with)\b", s, re.I):
        raise ValueError("Only SELECT queries are allowed")
    code_only = re.sub(r"'(?:[^']|'')*'", "''", s)  # ignore words inside quoted text
    if "public" in code_only.lower() or '"' in code_only:
        raise ValueError("Query only the students / student_events tables")
    bad = FORBIDDEN.search(code_only)
    if bad:
        raise ValueError(f"'{bad.group(0)}' is not allowed. Query only the students / student_events tables.")
    return s


def run(sql, ui_filters=None):
    """Execute a validated SELECT in a read-only transaction with a timeout. Returns rows/columns."""
    s = validate(sql)
    t0 = time.time()
    with student_data._connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute(f"SET LOCAL statement_timeout = {TIMEOUT_MS}")
            where, params = student_data._filtered_where(ui_filters)
            groups = _college_groups(cur, where, params)
            cur.execute("SELECT current_schema()")
            schema = cur.fetchone()[0]
            # From here on, unqualified names can't reach any real table: only our two virtual tables exist.
            cur.execute("SET LOCAL search_path = pg_catalog")
            where = where.replace("student_imports", f'"{schema}".student_imports')
            values = ", ".join(["(%s, %s)"] * len(groups)) or "(NULL::text, NULL::text)"
            gparams = [x for pair in groups for x in pair]
            wrapped = f"""WITH _college_map(raw, grp) AS (VALUES {values}),
students AS (
  SELECT r.id, r.name, r.mobile, r.alt_mobile, r.email, r.batch_name AS batch, r.college_state AS state, r.college,
         COALESCE(m.grp, r.college) AS college_group, r.mbbs_batch, r.company_options AS company, r.payment_mode, r.status,
         r.employee_name AS employee, COALESCE(r.total_fee,0) AS total_fee, COALESCE(r.discount_fee,0) AS discount,
         COALESCE(r.paid_amount,0) AS paid, COALESCE(r.due_amount,0) AS due, r.joining_date,
         to_char(r.joining_date, 'YYYY-MM') AS joining_month, r.due_date,
         COALESCE(r.attended_event_count,0) AS events_attended, COALESCE(r.attended_events,'[]'::jsonb) AS attended_events
  FROM "{schema}".student_records r LEFT JOIN _college_map m ON m.raw = r.college
  {where.replace('import_id', 'r.import_id')}
),
student_events AS (
  SELECT s.id AS student_id, s.name, s.batch, s.college, s.college_group, s.state, s.joining_date, e AS event
  FROM students s, jsonb_array_elements_text(s.attended_events) e
)
SELECT * FROM ({s.replace("%", "%%")}) AS _q LIMIT {MAX_ROWS + 1}"""
            cur.execute(wrapped, [*gparams, *params])
            cols = [d.name for d in cur.description]
            raw = cur.fetchall()
    rows = [{c: student_data._clean(v) for c, v in zip(cols, r)} for r in raw[:MAX_ROWS]]
    return {"columns": cols, "rows": rows, "row_count": len(rows), "truncated": len(raw) > MAX_ROWS,
            "ms": int((time.time() - t0) * 1000)}
