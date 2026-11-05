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
