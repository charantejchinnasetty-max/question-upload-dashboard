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

# Reference vocabulary used only to understand common NEET PG subject names in a
# question. Attendance itself is always calculated from the imported Excel data.
NEET_PG_SUBJECTS = {
    "Anatomy": ("anatomy",), "Physiology": ("physiology",), "Biochemistry": ("biochemistry", "biochem"),
    "Pharmacology": ("pharmacology", "pharma"), "Pathology": ("pathology", "path"),
    "Microbiology": ("microbiology", "micro"), "Forensic Medicine": ("forensic", "f.m", "fm"),
    "PSM": ("psm", "community medicine"), "ENT": ("ent",), "Ophthalmology": ("ophthalmology", "ophthal"),
    "Medicine": ("medicine",), "Surgery": ("surgery",), "Obstetrics & Gynaecology": ("obg", "obstetrics", "gynaecology", "gynecology"),
    "Paediatrics": ("paediatrics", "pediatrics"), "Orthopaedics": ("orthopaedics", "orthopedics", "ortho"),
    "Dermatology": ("dermatology", "derma"), "Psychiatry": ("psychiatry",), "Radiology": ("radiology", "radio"),
    "Anaesthesia": ("anaesthesia", "anesthesia", "anaesth"),
}


def _agent_subject(question):
    """Recognize subject aliases such as micro, pharma and derma."""
    lower = question.lower()
    compact = re.sub(r"[^a-z0-9]", "", lower)
    for canonical, aliases in NEET_PG_SUBJECTS.items():
        if any((re.search(r"\b" + re.escape(alias.lower()) + r"\b", lower) if len(re.sub(r"[^a-z0-9]", "", alias)) <= 3
                else re.sub(r"[^a-z0-9]", "", alias) in compact) for alias in aliases):
            return canonical, aliases
    return None, ()


def _agent_batch(cursor, where, params, question):
    """Find a real imported batch referenced anywhere in the user's question."""
    batch_rows = _rows(cursor, f"SELECT DISTINCT batch_name FROM student_records {where} AND batch_name IS NOT NULL", params)
    compact_question = re.sub(r"[^a-z0-9]", "", question.lower())
    exact = next((row["batch_name"] for row in batch_rows
                  if re.sub(r"[^a-z0-9]", "", row["batch_name"].lower()) in compact_question), None)
    if exact:
        return exact
    short_codes = [re.sub(r"[^a-z0-9]", "", code) for code in re.findall(r"ssp\s*\d+\s*\(?[a-z]\)?", question.lower())]
    return next((row["batch_name"] for row in batch_rows if any(
        re.sub(r"[^a-z0-9]", "", row["batch_name"].lower()).startswith(code) for code in short_codes)), None)


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


DATE_FORMATS = (
    "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%Y/%m/%d", "%d/%m/%y", "%d-%m-%y", "%d.%m.%y",
    "%d-%b-%Y", "%d-%b-%y", "%d %b %Y", "%d %B %Y", "%d-%B-%Y", "%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y",
    "%d %b, %Y", "%d %B, %Y", "%Y.%m.%d",
)


def _date(value):
    """Read a date in any common Excel/Indian format (dd/mm first), Excel serial numbers and timestamps."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _excel_serial(value)
    value = _text(value)
    if not value:
        return None
    if re.fullmatch(r"\d{5}(\.\d+)?", value):
        return _excel_serial(float(value))
    v = re.sub(r"\s+", " ", value.strip())
    v = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", v, flags=re.I)          # 7th Oct 2025
    v = re.split(r"[T ](?=\d{1,2}:\d{2})", v)[0].strip()                # drop time part
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    try:  # US style only when dd/mm is impossible (e.g. 10/25/2025)
        return datetime.strptime(v, "%m/%d/%Y").date()
    except ValueError:
        return None


def _excel_serial(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return None
    if not 20000 <= n <= 80000:  # roughly 1954–2119; anything else isn't a date serial
        return None
    from datetime import timedelta
    return date(1899, 12, 30) + timedelta(days=int(n))


JOINING_KEYS = ("Joining_date", "Joining Date", "Date of Joining", "DOJ", "Join Date", "Joined On", "Joined_on", "Admission Date",
                "Admission_date", "Date of Admission", "Enrolled On", "Enrollment Date", "Enrolment Date", "Created At", "Created_at")
DUE_KEYS = ("Due_date", "Due Date", "Fee Due Date", "Next Due Date")


def _pick(raw, keys):
    """Value of the first matching column, ignoring case, spaces and underscores."""
    norm = {re.sub(r"[^a-z0-9]", "", str(k).lower()): v for k, v in raw.items()}
    for k in keys:
        v = norm.get(re.sub(r"[^a-z0-9]", "", k.lower()))
        if v is not None and str(v).strip():
            return v
    return None


_dates_repaired = False


def repair_dates(force=False):
    """Re-read joining/due dates from the stored original rows (raw_record) for rows saved before the
    date reader understood every format. Safe to run repeatedly; only fills empty dates."""
    global _dates_repaired
    if _dates_repaired and not force:
        return 0
    fixed = 0
    with _connection() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT to_regclass('student_records')")
        if cursor.fetchone()[0] is None:
            _dates_repaired = True
            return 0
        cursor.execute("SELECT id, raw_record, joining_date, due_date FROM student_records WHERE joining_date IS NULL OR due_date IS NULL")
        updates = []
        for rid, raw, jd, dd in cursor.fetchall():
            raw = raw if isinstance(raw, dict) else json.loads(raw)
            new_j = jd or _date(_pick(raw, JOINING_KEYS))
            new_d = dd or _date(_pick(raw, DUE_KEYS))
            if new_j != jd or new_d != dd:
                updates.append((new_j, new_d, rid))
        if updates:
            cursor.executemany("UPDATE student_records SET joining_date = %s, due_date = %s WHERE id = %s", updates)
            fixed = len(updates)
    _dates_repaired = True
    return fixed


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
            _date(_pick(raw, JOINING_KEYS)), _date(_pick(raw, DUE_KEYS)), attended_count, json.dumps(_events(raw.get("Attended_events")))
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


def _agent_table_rows(records):
    """Return a deliberately small, display-safe student table for the assistant."""
    return [{
        "Name": row.get("name") or "Not specified",
        "Mobile": row.get("mobile") or "—",
        "Batch": row.get("batch_name") or "Not specified",
        "College": row.get("college") or "Not specified",
        "State": row.get("college_state") or "Not specified",
        "Paid": _clean(row.get("paid_amount")) or 0,
        "Due": _clean(row.get("due_amount")) or 0,
        "Event count": row.get("attended_event_count") or 0,
        "Subjects": row.get("attended_events") or [],
    } for row in records]


def agent_answer(question, filters=None):
    """Answer supported natural-language questions from the active dataset only.

    This intentionally uses deterministic, parameterized queries instead of an LLM
    so figures and student details are always traceable to imported records.
    """
    question = _text(question)
    if not question:
        raise ValueError("Ask a question about students, batches, colleges, fees, dues, or attendance.")
    filters = dict(filters or {})
    lower = question.lower()
    mobile_match = re.search(r"(?<!\d)(\d{7,15})(?!\d)", question)
    where, params = _filtered_where(filters)

    with _connection() as connection, connection.cursor() as cursor:
        # A mobile number is an unambiguous profile lookup. It takes precedence over reports.
        if mobile_match:
            mobile = mobile_match.group(1)
            profile_where = where + " AND mobile_normalized = %s"
            profile_params = [*params, mobile]
            records = _rows(cursor, f"""SELECT name, mobile, email, batch_name, college, college_state,
                mbbs_batch, status, total_fee, discount_fee, paid_amount, due_amount,
                attended_event_count, attended_events FROM student_records {profile_where}
                ORDER BY source_row_number LIMIT 10""", profile_params)
            if not records:
                return {"answer": f"No active student record matched mobile number {mobile} with the current dashboard filters.", "rows": [], "columns": [], "chart": None}
            first = records[0]
            subjects = first.get("attended_events") or []
            answer = (f"{first.get('name') or 'This student'} is in {first.get('batch_name') or 'an unspecified batch'} "
                      f"at {first.get('college') or 'an unspecified college'}. They have attended "
                      f"{first.get('attended_event_count') or 0} events, paid {first.get('paid_amount') or 0}, "
                      f"and have {first.get('due_amount') or 0} due.")
            if any(word in lower for word in ("subject", "class", "attend", "event")):
                answer += " Attended subjects/classes: " + (", ".join(subjects) if subjects else "none recorded in the imported file") + "."
            return {"answer": answer, "rows": _agent_table_rows(records), "columns": ["Name", "Mobile", "Batch", "College", "Paid", "Due", "Event count", "Subjects"], "chart": None}

        # Data-driven batch, subject and academic questions. A subject can be named
        # anywhere in a sentence: "SSP2B derma class", "who attended micro", etc.
        batch_name = _agent_batch(cursor, where, params, question)
        canonical_subject, aliases = _agent_subject(question)
        subject_match = re.search(r"(?:attended|attend|attendance\s+(?:for|in))\s+([a-z][a-z0-9 .&/\-]*?)(?=\s+(?:in|from|for)\s+|[?.!,]|$)", lower)
        if not canonical_subject and subject_match:
            candidate = _text(subject_match.group(1))
            if candidate and candidate not in {"event", "events", "class", "classes", "attendance"}:
                canonical_subject, aliases = candidate.title(), (candidate,)

        subject_where, subject_params = where, list(params)
        if batch_name:
            subject_where += " AND batch_name = %s"
            subject_params.append(batch_name)

        if canonical_subject:
            event_conditions = " OR ".join("event_name ILIKE %s" for _ in aliases)
            records = _rows(cursor, f"""SELECT name, mobile, batch_name, college, college_state,
                paid_amount, due_amount, attended_event_count, attended_events FROM student_records {subject_where}
                AND EXISTS (SELECT 1 FROM jsonb_array_elements_text(attended_events) AS event_name WHERE {event_conditions})
                ORDER BY name NULLS LAST LIMIT 500""", [*subject_params, *[f"%{alias}%" for alias in aliases]])
            label = f" in {batch_name}" if batch_name else ""
            return {"answer": f"Exactly {len(records)} students have {canonical_subject} recorded in their attended events{label}. The matching students are listed below.", "rows": _agent_table_rows(records), "columns": ["Name", "Mobile", "Batch", "College", "Event count", "Subjects"], "chart": None}

        if batch_name and any(word in lower for word in ("completed", "complete", "pending", "remaining", "academic", "class", "classes", "subject", "subjects")):
            event_rows = _rows(cursor, f"SELECT attended_events FROM student_records {subject_where}", subject_params)
            imported_events = " ".join(event for row in event_rows for event in (row.get("attended_events") or [])).lower()
            status_rows = []
            for subject, subject_aliases in NEET_PG_SUBJECTS.items():
                recorded = any(alias.lower() in imported_events for alias in subject_aliases)
                status_rows.append({"Subject": subject, "Attendance evidence": "Recorded" if recorded else "Not recorded"})
            completed = sum(row["Attendance evidence"] == "Recorded" for row in status_rows)
            return {"answer": f"{batch_name} has attendance evidence for {completed} of the {len(NEET_PG_SUBJECTS)} NEET PG subjects. Subjects marked Not recorded are not yet evidenced in this Excel import; a true pending-class count needs a planned class schedule or timetable in the source data.", "rows": status_rows, "columns": ["Subject", "Attendance evidence"], "chart": None}

        if batch_name and any(word in lower for word in ("student", "students", "member", "members", "who", "list", "show")):
            records = _rows(cursor, f"""SELECT name, mobile, batch_name, college, college_state, paid_amount,
                due_amount, attended_event_count, attended_events FROM student_records {subject_where}
                ORDER BY name NULLS LAST LIMIT 500""", subject_params)
            return {"answer": f"{len(records)} students are in {batch_name} under the current filters.", "rows": _agent_table_rows(records), "columns": ["Name", "Mobile", "Batch", "College", "Paid", "Due", "Event count", "Subjects"], "chart": None}

        # College, state and batch questions return group reports that respect active filters.
        if any(word in lower for word in ("college", "colleges")):
            rows = _rows(cursor, f"""SELECT COALESCE(NULLIF(college, ''), 'Not specified') AS label,
                COUNT(*) AS students, COALESCE(SUM(paid_amount), 0) AS paid_amount,
                COALESCE(SUM(due_amount), 0) AS due_amount
                FROM student_records {where} GROUP BY 1 ORDER BY students DESC, label ASC LIMIT 50""", params)
            clean = [{key: _clean(value) for key, value in row.items()} for row in rows]
            return {"answer": f"Found {len(clean)} colleges in the active filtered dataset. Select a college row to review its student count and financial totals.", "rows": clean, "columns": ["label", "students", "paid_amount", "due_amount"], "chart": {"label": "College", "items": clean[:10]}}
        if any(word in lower for word in ("state", "states", "location", "geography")):
            rows = _rows(cursor, f"""SELECT COALESCE(NULLIF(college_state, ''), 'Not specified') AS label,
                COUNT(*) AS students, COALESCE(SUM(paid_amount), 0) AS paid_amount,
                COALESCE(SUM(due_amount), 0) AS due_amount
                FROM student_records {where} GROUP BY 1 ORDER BY students DESC, label ASC LIMIT 50""", params)
            clean = [{key: _clean(value) for key, value in row.items()} for row in rows]
            return {"answer": f"Found {len(clean)} states in the active filtered dataset.", "rows": clean, "columns": ["label", "students", "paid_amount", "due_amount"], "chart": {"label": "State", "items": clean[:10]}}
        if any(word in lower for word in ("batch", "batches")):
            rows = _rows(cursor, f"""SELECT COALESCE(NULLIF(batch_name, ''), 'Not specified') AS label,
                COUNT(*) AS students, COALESCE(SUM(paid_amount), 0) AS paid_amount,
                COALESCE(SUM(due_amount), 0) AS due_amount, COALESCE(SUM(attended_event_count), 0) AS events
                FROM student_records {where} GROUP BY 1 ORDER BY students DESC, label ASC LIMIT 50""", params)
            clean = [{key: _clean(value) for key, value in row.items()} for row in rows]
            return {"answer": f"Found {len(clean)} batches in the active filtered dataset.", "rows": clean, "columns": ["label", "students", "paid_amount", "due_amount", "events"], "chart": {"label": "Batch", "items": clean[:10]}}

        # Attendance questions list exact student results, never a made-up class total.
        if any(word in lower for word in ("attendance", "attended", "subject", "subjects", "class", "classes", "event", "events")):
            attendance_where = where + " AND COALESCE(attended_event_count, 0) > 0"
            records = _rows(cursor, f"""SELECT name, mobile, batch_name, college, college_state, paid_amount,
                due_amount, attended_event_count, attended_events FROM student_records {attendance_where}
                ORDER BY attended_event_count DESC, name NULLS LAST LIMIT 100""", params)
            total = sum(int(row.get("attended_event_count") or 0) for row in records)
            return {"answer": f"{len(records)} matching students have attendance recorded. The displayed students total {total} attended events; open a student's subject list for the imported class names.", "rows": _agent_table_rows(records), "columns": ["Name", "Mobile", "Batch", "College", "Event count", "Subjects"], "chart": None}

        summary = _rows(cursor, f"""SELECT COUNT(*) AS students, COALESCE(SUM(total_fee), 0) AS total_fee,
            COALESCE(SUM(paid_amount), 0) AS paid_amount, COALESCE(SUM(due_amount), 0) AS due_amount,
            COUNT(*) FILTER (WHERE COALESCE(due_amount, 0) > 0) AS students_with_due,
            COUNT(*) FILTER (WHERE COALESCE(attended_event_count, 0) > 0) AS students_with_attendance,
            COALESCE(SUM(attended_event_count), 0) AS total_events FROM student_records {where}""", params)[0]
    clean = {key: _clean(value) for key, value in summary.items()}
    return {"answer": (f"For the current dashboard filters there are {clean['students']} students, total fee {clean['total_fee']}, "
                       f"paid {clean['paid_amount']}, due {clean['due_amount']}, and {clean['total_events']} attended events. "
                       "Ask about a batch, college, state, mobile number, fees, dues, or attendance for a detailed result."),
            "rows": [clean], "columns": list(clean), "chart": None}
