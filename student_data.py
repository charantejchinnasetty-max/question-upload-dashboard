"""Database-backed import and analytics helpers for Student Dashboard."""
import json
import os
import re
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

from openpyxl import load_workbook

REQUIRED_HEADERS = {"Name", "Mobile", "Batch_Name"}
PLACEHOLDERS = {"", "---", "others", "none", "null", "nan"}


def _driver():
    """Load the PostgreSQL driver only when Student Dashboard is used."""
    try:
        import psycopg
        return psycopg
    except ImportError as exc:
        raise RuntimeError("PostgreSQL driver is not installed. Run pip install -r requirements.txt.") from exc


def _connection():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not configured.")
    return _driver().connect(url)


def _text(value):
    if value is None:
        return None
    value = str(value).strip()
    return None if value.lower() in PLACEHOLDERS else value


def _phone(value):
    value = _text(value)
    if not value:
        return None
    return re.sub(r"\D", "", value)


def _amount(value):
    value = _text(value)
    if not value:
        return None
    try:
        return Decimal(re.sub(r"[^0-9.-]", "", value))
    except (InvalidOperation, ValueError):
        return None


def _date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    value = _text(value)
    if not value:
        return None
    for pattern in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(value, pattern).date()
        except ValueError:
            continue
    return None


def _events(value):
    value = _text(value)
    return [item.strip() for item in value.split(",") if item.strip()] if value else []


def initialize():
    """Create the traceable import tables without overwriting source data."""
    with _connection() as connection, connection.cursor() as cursor:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS student_imports (
                id UUID PRIMARY KEY,
                filename TEXT NOT NULL,
                source_rows INTEGER NOT NULL,
                imported_at TIMESTAMPTZ NOT NULL,
                is_active BOOLEAN NOT NULL DEFAULT TRUE
            );
            CREATE TABLE IF NOT EXISTS student_records (
                id UUID PRIMARY KEY,
                import_id UUID NOT NULL REFERENCES student_imports(id) ON DELETE CASCADE,
                source_row_number INTEGER NOT NULL,
                raw_record JSONB NOT NULL,
                name TEXT,
                mobile TEXT,
                mobile_normalized TEXT,
                alt_mobile TEXT,
                email TEXT,
                batch_name TEXT,
                college_state TEXT,
                college TEXT,
                mbbs_batch TEXT,
                company_options TEXT,
                payment_mode TEXT,
                status TEXT,
                employee_name TEXT,
                total_fee NUMERIC,
                discount_fee NUMERIC,
                paid_amount NUMERIC,
                due_amount NUMERIC,
                joining_date DATE,
                due_date DATE,
                attended_event_count INTEGER,
                attended_events JSONB NOT NULL DEFAULT '[]'::jsonb
            );
            CREATE INDEX IF NOT EXISTS student_records_import_idx ON student_records(import_id);
            CREATE INDEX IF NOT EXISTS student_records_mobile_idx ON student_records(mobile_normalized);
            CREATE INDEX IF NOT EXISTS student_records_batch_idx ON student_records(batch_name);
            CREATE INDEX IF NOT EXISTS student_records_state_idx ON student_records(college_state);
            CREATE INDEX IF NOT EXISTS student_records_college_idx ON student_records(college);
        """)


def import_workbook(source, filename, replace=False):
    """Create a versioned import; old datasets remain available for rollback."""
    workbook = load_workbook(source, read_only=True, data_only=True)
    sheet = workbook.active
    rows = sheet.iter_rows(values_only=True)
    headers = next(rows, None)
    if not headers:
        raise ValueError("The Excel file is empty.")
    headers = [str(header).strip() if header is not None else "" for header in headers]
    missing = sorted(REQUIRED_HEADERS - set(headers))
    if missing:
        raise ValueError(f"Missing required columns: {', '.join(missing)}")
    import_id = uuid.uuid4()
    prepared = []
    for source_row_number, row in enumerate(rows, 2):
        raw = {headers[index]: row[index] if index < len(row) else None for index in range(len(headers))}
        if not any(value is not None and str(value).strip() for value in raw.values()):
            continue
        attended = _text(raw.get("Attended_event_count"))
        try:
            attended_count = int(float(attended)) if attended is not None else None
        except ValueError:
            attended_count = None
        prepared.append((
            uuid.uuid4(), import_id, source_row_number, json.dumps(raw, default=str), _text(raw.get("Name")),
            _text(raw.get("Mobile")), _phone(raw.get("Mobile")), _text(raw.get("Alt_Mobile")), _text(raw.get("Email")),
            _text(raw.get("Batch_Name")), _text(raw.get("College State")), _text(raw.get("College")), _text(raw.get("Mbbs_Batch")),
            _text(raw.get("Company_options")), _text(raw.get("Payment_mode")), _text(raw.get("Status")), _text(raw.get("Employee Name")),
            _amount(raw.get("Total_fee")), _amount(raw.get("Discount_fee")), _amount(raw.get("Paid_amount")), _amount(raw.get("Due_amount")),
            _date(raw.get("Joining_date")), _date(raw.get("Due_date")), attended_count, json.dumps(_events(raw.get("Attended_events")))
        ))
    if not prepared:
        raise ValueError("No student records were found in the Excel file.")
    initialize()
    with _connection() as connection, connection.cursor() as cursor:
        if replace:
            cursor.execute("UPDATE student_imports SET is_active = FALSE WHERE is_active = TRUE")
        cursor.execute("INSERT INTO student_imports (id, filename, source_rows, imported_at, is_active) VALUES (%s, %s, %s, %s, TRUE)", (import_id, filename, len(prepared), datetime.now(timezone.utc)))
        cursor.executemany("""
            INSERT INTO student_records (id, import_id, source_row_number, raw_record, name, mobile, mobile_normalized, alt_mobile, email, batch_name, college_state, college, mbbs_batch, company_options, payment_mode, status, employee_name, total_fee, discount_fee, paid_amount, due_amount, joining_date, due_date, attended_event_count, attended_events)
            VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
        """, prepared)
    return {"import_id": str(import_id), "rows": len(prepared), "headers": headers}


def _filtered_where(filters):
    """Build the shared filter clause for every student report."""
    filters = filters or {}
    clauses = ["import_id IN (SELECT id FROM student_imports WHERE is_active = TRUE)"]
    params = []
    search = _text(filters.get("search")); state = _text(filters.get("state"))
    batch = _text(filters.get("batch")); college = _text(filters.get("college")); status = _text(filters.get("status"))
    if search:
        clauses.append("(name ILIKE %s OR mobile ILIKE %s OR email ILIKE %s OR college ILIKE %s)")
        params.extend([f"%{search}%"] * 4)
    for column, value in (("college_state", state), ("batch_name", batch), ("college", college), ("status", status)):
        if value:
            clauses.append(f"{column} = %s"); params.append(value)
    if _text(filters.get("due_only")):
        clauses.append("COALESCE(due_amount, 0) > 0")
    if _text(filters.get("attendance_only")):
        clauses.append("COALESCE(attended_event_count, 0) > 0")
    return " WHERE " + " AND ".join(clauses), params


def _clean(value):
    if isinstance(value, Decimal): return float(value)
    if isinstance(value, date): return value.isoformat()
    return value


def _rows(cursor, query, query_params=()):
    cursor.execute(query, query_params)
    names = [item.name for item in cursor.description]
    return [dict(zip(names, record)) for record in cursor.fetchall()]


def dashboard_data(filters=None):
    """Return live overview data calculated only from active student imports."""
    where, params = _filtered_where(filters)

    with _connection() as connection, connection.cursor() as cursor:
        summary = _rows(cursor, f"""
            SELECT COUNT(*) AS students, COALESCE(SUM(total_fee), 0) AS total_fee,
                   COALESCE(SUM(paid_amount), 0) AS paid_amount, COALESCE(SUM(due_amount), 0) AS due_amount,
                   COALESCE(SUM(discount_fee), 0) AS discounts, COUNT(DISTINCT NULLIF(college, '')) AS colleges,
                   COUNT(DISTINCT NULLIF(batch_name, '')) AS batches,
                   COUNT(*) FILTER (WHERE COALESCE(due_amount, 0) > 0) AS with_dues,
                   COUNT(*) FILTER (WHERE COALESCE(attended_event_count, 0) > 0) AS with_attendance,
                   COALESCE(SUM(attended_event_count), 0) AS total_attendances,
                   COUNT(*) FILTER (WHERE LOWER(COALESCE(status, '')) IN ('active', 'paid', 'completed')) AS active_students
            FROM student_records {where}
        """, params)[0]
        batches = _rows(cursor, f"""SELECT COALESCE(NULLIF(batch_name, ''), 'Not specified') AS label, COUNT(*) AS students,
                   COALESCE(SUM(paid_amount), 0) AS paid_amount, COALESCE(SUM(due_amount), 0) AS due_amount
            FROM student_records {where} GROUP BY 1 ORDER BY students DESC, label ASC LIMIT 8""", params)
        states = _rows(cursor, f"""SELECT COALESCE(NULLIF(college_state, ''), 'Not specified') AS label, COUNT(*) AS students
            FROM student_records {where} GROUP BY 1 ORDER BY students DESC, label ASC LIMIT 8""", params)
        filter_values = {
            "states": _rows(cursor, "SELECT DISTINCT college_state AS label FROM student_records WHERE import_id IN (SELECT id FROM student_imports WHERE is_active = TRUE) AND college_state IS NOT NULL AND college_state <> '' ORDER BY label"),
            "batches": _rows(cursor, "SELECT DISTINCT batch_name AS label FROM student_records WHERE import_id IN (SELECT id FROM student_imports WHERE is_active = TRUE) AND batch_name IS NOT NULL AND batch_name <> '' ORDER BY label"),
            "colleges": _rows(cursor, "SELECT DISTINCT college AS label FROM student_records WHERE import_id IN (SELECT id FROM student_imports WHERE is_active = TRUE) AND college IS NOT NULL AND college <> '' ORDER BY label"),
            "statuses": _rows(cursor, "SELECT DISTINCT status AS label FROM student_records WHERE import_id IN (SELECT id FROM student_imports WHERE is_active = TRUE) AND status IS NOT NULL AND status <> '' ORDER BY label"),
        } if _text((filters or {}).get("include_filters")) else {}
    return {"summary": {key: _clean(value) for key, value in summary.items()},
            "batches": [{key: _clean(value) for key, value in row.items()} for row in batches],
            "states": [{key: _clean(value) for key, value in row.items()} for row in states], "filters": filter_values}


def college_counts(filters=None):
    """Return every college with its live count, then allow a second click into students."""
    where, params = _filtered_where(filters)
    with _connection() as connection, connection.cursor() as cursor:
        results = _rows(cursor, f"""SELECT COALESCE(NULLIF(college, ''), 'Not specified') AS label,
             COUNT(*) AS students, COALESCE(SUM(paid_amount), 0) AS paid_amount,
             COALESCE(SUM(due_amount), 0) AS due_amount
             FROM student_records {where} GROUP BY 1 ORDER BY students DESC, label ASC""", params)
    return [{key: _clean(value) for key, value in row.items()} for row in results]


def student_records(filters=None):
    """Return the complete matching student list for a click-through view."""
    where, params = _filtered_where(filters)
    with _connection() as connection, connection.cursor() as cursor:
        records = _rows(cursor, f"""SELECT name, mobile, email, batch_name, college, college_state, mbbs_batch, status,
             total_fee, discount_fee, paid_amount, due_amount, attended_event_count, attended_events
             FROM student_records {where} ORDER BY name NULLS LAST, source_row_number""", params)
    return [{key: _clean(value) for key, value in row.items()} for row in records]
