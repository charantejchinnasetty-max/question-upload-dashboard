"""Student AI Agent — an LLM (Claude) that answers any question about the imported student data.

How it stays trustworthy:
- The model never sees the database directly. It can only call the read-only tools below,
  which build parameterized SQL from a whitelist of columns and operators.
- Every query is scoped to the active import and to the filters selected in the UI.
- Numbers in the answer must come from tool results (enforced by the system prompt, and the
  UI shows the exact table the answer was based on).
- Without ANTHROPIC_API_KEY, the app falls back to the deterministic agent in student_data.py.
"""
import json
import os
import re
from datetime import date, datetime
from decimal import Decimal

import student_data

MODEL = os.environ.get("AI_MODEL", "claude-sonnet-5-5")
MAX_TOOL_ROUNDS = 8
MAX_ROWS_TO_MODEL = 150

# Whitelisted columns: name -> (sql expression, type, description)
FIELDS = {
    "name": ("name", "text", "Student name"),
    "mobile": ("mobile", "text", "Mobile number as imported"),
    "email": ("email", "text", "Email"),
    "batch": ("batch_name", "text", "Course batch name, e.g. 'SSP2 (B) Achievers'"),
    "state": ("college_state", "text", "State of the student's medical college"),
    "college": ("college", "text", "Medical college name"),
    "mbbs_batch": ("mbbs_batch", "text", "MBBS admission year/batch"),
    "company": ("company_options", "text", "Company (e.g. ASC Edtech / DBMCI Hyderabad)"),
    "payment_mode": ("payment_mode", "text", "Payment mode"),
    "status": ("status", "text", "Student status as imported"),
    "employee": ("employee_name", "text", "Employee / counsellor who enrolled the student"),
    "total_fee": ("total_fee", "number", "Total fee (INR)"),
    "discount": ("discount_fee", "number", "Discount given (INR)"),
    "paid": ("paid_amount", "number", "Amount paid (INR)"),
    "due": ("due_amount", "number", "Amount due (INR)"),
    "joining_date": ("joining_date", "date", "Joining date"),
    "due_date": ("due_date", "date", "Fee due date"),
    "joining_month": ("to_char(joining_date, 'YYYY-MM')", "text", "Joining month YYYY-MM (for grouping)"),
    "events_attended": ("attended_event_count", "number", "Number of classes/events attended"),
    "attended_events": ("attended_events", "events", "List of attended class/event names"),
}
TEXT_OPS = {"eq", "neq", "contains", "not_contains", "in", "not_in", "is_empty", "not_empty", "starts_with"}
NUM_OPS = {"eq", "neq", "gt", "gte", "lt", "lte", "between", "in", "is_empty", "not_empty"}
EVENT_OPS = {"has_event", "lacks_event", "has_all_events"}
METRICS = {"count", "sum", "avg", "min", "max", "count_distinct"}


def _clean(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    return v


def _rows(cursor, sql, params):
    cursor.execute(sql, params)
    names = [d.name for d in cursor.description]
    return [{k: _clean(v) for k, v in zip(names, r)} for r in cursor.fetchall()]


class ToolError(ValueError):
    pass


def _field(name, allow_events=False):
    f = FIELDS.get(name)
    if not f:
        raise ToolError(f"Unknown field '{name}'. Valid fields: {', '.join(FIELDS)}")
    if f[1] == "events" and not allow_events:
        raise ToolError("attended_events can only be used with has_event / lacks_event filters or event_breakdown")
    return f


def _filter_sql(filters):
    """Translate model-supplied filters into parameterized SQL. Raises ToolError on anything unexpected."""
    clauses, params = [], []
    for f in filters or []:
        if not isinstance(f, dict):
            raise ToolError("Each filter must be an object {field, op, value}")
        field, op, value = f.get("field"), f.get("op"), f.get("value")
        expr, typ, _ = _field(field, allow_events=True)
        if typ == "events":
            if op not in EVENT_OPS:
                raise ToolError(f"attended_events supports {sorted(EVENT_OPS)}")
            terms = value if isinstance(value, list) else [value]
            terms = [str(t) for t in terms if str(t or "").strip()]
            if not terms:
                raise ToolError("Give at least one event/subject name")
            one = "EXISTS (SELECT 1 FROM jsonb_array_elements_text(attended_events) e WHERE e ILIKE %s)"
            if op == "has_event":
                clauses.append("(" + " OR ".join([one] * len(terms)) + ")")
            elif op == "has_all_events":
                clauses.append("(" + " AND ".join([one] * len(terms)) + ")")
            else:
                clauses.append("NOT (" + " OR ".join([one] * len(terms)) + ")")
            params += [f"%{t}%" for t in terms]
            continue
        ops = NUM_OPS if typ in ("number", "date") else TEXT_OPS
        if op not in ops:
            raise ToolError(f"Operator '{op}' not allowed for {field}. Allowed: {sorted(ops)}")
        if op == "is_empty":
            clauses.append(f"({expr} IS NULL" + (f" OR {expr} = ''" if typ == "text" else "") + ")")
        elif op == "not_empty":
            clauses.append(f"({expr} IS NOT NULL" + (f" AND {expr} <> ''" if typ == "text" else "") + ")")
        elif op in ("in", "not_in"):
            vals = value if isinstance(value, list) else [value]
            if not vals:
                raise ToolError("'in' needs a list of values")
            if typ == "text":
                clauses.append(f"{'NOT ' if op == 'not_in' else ''}LOWER({expr}) = ANY(%s)")
                params.append([str(v).lower() for v in vals])
            else:
                clauses.append(f"{expr} {'<> ALL' if op == 'not_in' else '= ANY'}(%s)")
                params.append(vals)
        elif op == "between":
            if not isinstance(value, list) or len(value) != 2:
                raise ToolError("'between' needs [low, high]")
            clauses.append(f"{expr} BETWEEN %s AND %s")
            params += value
        elif typ == "text":
            if op == "eq":
                clauses.append(f"LOWER({expr}) = LOWER(%s)"); params.append(str(value))
            elif op == "neq":
                clauses.append(f"COALESCE(LOWER({expr}), '') <> LOWER(%s)"); params.append(str(value))
            elif op == "contains":
                clauses.append(f"{expr} ILIKE %s"); params.append(f"%{value}%")
            elif op == "not_contains":
                clauses.append(f"COALESCE({expr}, '') NOT ILIKE %s"); params.append(f"%{value}%")
            elif op == "starts_with":
                clauses.append(f"{expr} ILIKE %s"); params.append(f"{value}%")
        else:
            sym = {"eq": "=", "neq": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}[op]
            if typ == "number":
                clauses.append(f"COALESCE({expr}, 0) {sym} %s")
            else:
                clauses.append(f"{expr} {sym} %s")
            params.append(value)
    return clauses, params


def _scoped_where(ui_filters, filters):
    base, base_params = student_data._filtered_where(ui_filters)
    extra, extra_params = _filter_sql(filters)
    where = base + "".join(f" AND {c}" for c in extra)
    return where, [*base_params, *extra_params]


# ───────────── Tools ─────────────

def tool_dataset_overview(cursor, ui_filters, _args):
    where, params = student_data._filtered_where(ui_filters)
    summary = _rows(cursor, f"""SELECT COUNT(*) students, COALESCE(SUM(total_fee),0) total_fee, COALESCE(SUM(paid_amount),0) paid,
        COALESCE(SUM(due_amount),0) due, COUNT(*) FILTER (WHERE COALESCE(due_amount,0)>0) students_with_due,
        MIN(joining_date) first_joining, MAX(joining_date) last_joining FROM student_records {where}""", params)[0]
    def top(col, n=40):
        return _rows(cursor, f"SELECT COALESCE(NULLIF({col},''),'(blank)') v, COUNT(*) n FROM student_records {where} GROUP BY 1 ORDER BY n DESC LIMIT {n}", params)
    events = _rows(cursor, f"""SELECT e v, COUNT(DISTINCT id) n FROM student_records, jsonb_array_elements_text(attended_events) e
        {where} GROUP BY 1 ORDER BY n DESC LIMIT 60""", params)
    imp = _rows(cursor, "SELECT filename, source_rows, imported_at FROM student_imports WHERE is_active ORDER BY imported_at DESC LIMIT 5", [])
    return {
        "summary": summary, "active_imports": imp,
        "batches": top("batch_name"), "states": top("college_state"), "statuses": top("status", 20),
        "companies": top("company_options", 10), "mbbs_batches": top("mbbs_batch", 20), "employees": top("employee_name", 30),
        "colleges_top": top("college", 25), "event_names": events,
        "fields": {k: f"{v[1]} — {v[2]}" for k, v in FIELDS.items()},
    }


def tool_find_values(cursor, ui_filters, args):
    field = args.get("field")
    expr, typ, _ = _field(field, allow_events=True)
    term = str(args.get("search") or "").strip()
    where, params = student_data._filtered_where(ui_filters)
    if typ == "events":
        sql = f"SELECT e v, COUNT(DISTINCT id) n FROM student_records, jsonb_array_elements_text(attended_events) e {where}"
        if term:
            sql += " AND e ILIKE %s"; params = [*params, f"%{term}%"]
        return {"values": _rows(cursor, sql + " GROUP BY 1 ORDER BY n DESC LIMIT 50", params)}
    sql = f"SELECT {expr} v, COUNT(*) n FROM student_records {where} AND {expr} IS NOT NULL"
    if term:
        compact = re.sub(r"[^a-z0-9]", "", term.lower())
        sql += f" AND ({expr}::text ILIKE %s OR regexp_replace(LOWER({expr}::text), '[^a-z0-9]', '', 'g') LIKE %s)"
        params = [*params, f"%{term}%", f"%{compact}%"]
    return {"values": _rows(cursor, sql + " GROUP BY 1 ORDER BY n DESC LIMIT 50", params)}


def tool_query_students(cursor, ui_filters, args):
    where, params = _scoped_where(ui_filters, args.get("filters"))
    group_by = args.get("group_by") or []
    limit = max(1, min(int(args.get("limit") or 50), 500))
    total = _rows(cursor, f"SELECT COUNT(*) n FROM student_records {where}", params)[0]["n"]

    if group_by:
        gexprs = []
        for g in group_by:
            expr, typ, _ = _field(g)
            gexprs.append((g, expr))
        sel = [f"COALESCE(NULLIF(({e})::text, ''), '(blank)') AS \"{g}\"" for g, e in gexprs]
        metrics = args.get("metrics") or [{"fn": "count"}]
        mnames = []
        for m in metrics:
            fn, fld = m.get("fn"), m.get("field")
            if fn not in METRICS:
                raise ToolError(f"Metric fn must be one of {sorted(METRICS)}")
            if fn == "count":
                sel.append('COUNT(*) AS "students"'); mnames.append("students"); continue
            expr, typ, _ = _field(fld)
            if fn != "count_distinct" and typ != "number":
                raise ToolError(f"{fn} needs a numeric field")
            alias = f"{fn}_{fld}"
            sel.append(f"{'COUNT(DISTINCT ' + expr + ')' if fn == 'count_distinct' else fn.upper() + '(' + expr + ')'} AS \"{alias}\"")
            mnames.append(alias)
        order = args.get("order_by") or mnames[0]
        if order not in mnames and order not in group_by:
            raise ToolError(f"order_by must be one of {mnames + group_by}")
        direction = "ASC" if str(args.get("order", "desc")).lower() == "asc" else "DESC"
        sql = (f"SELECT {', '.join(sel)} FROM student_records {where} GROUP BY {', '.join(str(i + 1) for i in range(len(gexprs)))} "
               f"ORDER BY \"{order}\" {direction} NULLS LAST LIMIT {limit}")
        rows = _rows(cursor, sql, params)
        for r in rows:
            for k, v in r.items():
                if isinstance(v, float):
                    r[k] = round(v, 2)
        return {"matching_students": total, "groups": len(rows), "rows": rows, "columns": [g for g, _ in gexprs] + mnames}

    cols = args.get("columns") or ["name", "mobile", "batch", "college", "state", "paid", "due", "events_attended"]
    sel = []
    for c in cols:
        expr, typ, _ = _field(c, allow_events=True)
        sel.append(f"{expr} AS \"{c}\"")
    order = args.get("order_by") or "name"
    oexpr = _field(order)[0]
    direction = "ASC" if str(args.get("order", "asc" if order == "name" else "desc")).lower() == "asc" else "DESC"
    rows = _rows(cursor, f"SELECT {', '.join(sel)} FROM student_records {where} ORDER BY {oexpr} {direction} NULLS LAST LIMIT {limit}", params)
    return {"matching_students": total, "returned": len(rows), "rows": rows, "columns": cols}


def tool_event_breakdown(cursor, ui_filters, args):
    where, params = _scoped_where(ui_filters, args.get("filters"))
    term = str(args.get("event_search") or "").strip()
    sql = f"SELECT e AS event, COUNT(DISTINCT id) AS students FROM student_records, jsonb_array_elements_text(attended_events) e {where}"
    if term:
        sql += " AND e ILIKE %s"; params = [*params, f"%{term}%"]
    rows = _rows(cursor, sql + f" GROUP BY 1 ORDER BY students DESC LIMIT {max(1, min(int(args.get('limit') or 40), 200))}", params)
    total = _rows(cursor, f"SELECT COUNT(*) n FROM student_records {_scoped_where(ui_filters, args.get('filters'))[0]}", _scoped_where(ui_filters, args.get("filters"))[1])[0]["n"]
    return {"students_in_scope": total, "rows": rows, "columns": ["event", "students"]}


def tool_get_student(cursor, ui_filters, args):
    q = str(args.get("query") or "").strip()
    if len(q) < 2:
        raise ToolError("Give a name or mobile number")
    where, params = student_data._filtered_where(ui_filters)
    digits = re.sub(r"\D", "", q)
    if len(digits) >= 6:
        where += " AND (mobile_normalized LIKE %s OR regexp_replace(COALESCE(alt_mobile,''), '\\D', '', 'g') LIKE %s)"
        params = [*params, f"%{digits[-10:]}", f"%{digits[-10:]}"]
    else:
        where += " AND (name ILIKE %s OR email ILIKE %s)"
        params = [*params, f"%{q}%", f"%{q}%"]
    rows = _rows(cursor, f"""SELECT name, mobile, alt_mobile, email, batch_name AS batch, college, college_state AS state, mbbs_batch,
        company_options AS company, payment_mode, status, employee_name AS employee, total_fee, discount_fee AS discount,
        paid_amount AS paid, due_amount AS due, joining_date, due_date, attended_event_count AS events_attended, attended_events
        FROM student_records {where} ORDER BY name LIMIT 10""", params)
    return {"matches": len(rows), "rows": rows, "columns": ["name", "mobile", "batch", "college", "state", "paid", "due", "events_attended", "attended_events"]}


TOOLS = {
    "dataset_overview": (tool_dataset_overview, "Overview of the active student dataset: totals, list of batches, states, statuses, companies, MBBS batches, employees, top colleges, the most common attended event/class names, and the available fields. Call this first when unsure what values exist.",
                         {"type": "object", "properties": {}}),
    "find_values": (tool_find_values, "Look up the exact stored values of a field that match a fuzzy search term (e.g. batch 'ssp2b' -> 'SSP2 (B) Achievers', college 'osmania', event 'micro'). Use before filtering when the user's wording may not match stored values exactly.",
                    {"type": "object", "properties": {"field": {"type": "string", "enum": list(FIELDS)}, "search": {"type": "string"}}, "required": ["field"]}),
    "query_students": (tool_query_students, "Count, list, or aggregate students. With group_by: returns one row per group with metrics (count, sum/avg/min/max of numeric fields, count_distinct). Without group_by: returns matching student rows with chosen columns. Always returns matching_students (the exact total count).",
                       {"type": "object", "properties": {
                           "filters": {"type": "array", "description": "AND-combined filters", "items": {"type": "object", "properties": {
                               "field": {"type": "string", "enum": list(FIELDS)},
                               "op": {"type": "string", "enum": sorted(TEXT_OPS | NUM_OPS | EVENT_OPS)},
                               "value": {"description": "string, number, ISO date, list for in/between, or event name(s) for has_event"}},
                               "required": ["field", "op"]}},
                           "group_by": {"type": "array", "items": {"type": "string", "enum": [k for k, v in FIELDS.items() if v[1] != "events"]}},
                           "metrics": {"type": "array", "items": {"type": "object", "properties": {"fn": {"type": "string", "enum": sorted(METRICS)}, "field": {"type": "string"}}, "required": ["fn"]}},
                           "columns": {"type": "array", "items": {"type": "string", "enum": list(FIELDS)}},
                           "order_by": {"type": "string", "description": "a column/group field, or a metric alias like 'students' or 'sum_due'"},
                           "order": {"type": "string", "enum": ["asc", "desc"]},
                           "limit": {"type": "integer", "minimum": 1, "maximum": 500}}}),
    "event_breakdown": (tool_event_breakdown, "Count students per attended class/event name (optionally only events matching event_search, and only students matching filters). Use for attendance-by-subject questions.",
                        {"type": "object", "properties": {"filters": {"type": "array", "items": {"type": "object"}}, "event_search": {"type": "string"}, "limit": {"type": "integer"}}}),
    "get_student": (tool_get_student, "Find a specific student by mobile number (any format) or by name/email, returning their full profile including fees and attended events.",
                    {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}),
}

SYSTEM = """You are the Student AI Agent for ASC Edtech / DBMCI Hyderabad (NEET PG / INI-CET coaching). Staff ask you questions about the imported student Excel data.

Rules:
1. Answer ONLY from tool results. Never guess or invent students, counts, amounts or names. If the data can't answer, say so plainly and say what data would be needed.
2. Exact counts come from `matching_students` / `matches` / group rows — never estimate.
3. The same college is often spelled several ways in the data ("Government Medical College, Kadapa", "GMC Kadapa", "Govt. Medical College Kadappa"). When the message includes "Pre-resolved from the data", filter with op "in" using ALL listed stored values, and mention that spelling variants were combined. "Joined"/"admissions" in a month means joining_date within that month.
3b. When the user's words might not match stored values (batch codes, college short names, subject nicknames like micro/pharma/derma/PSM/OBG), call find_values or dataset_overview first, then filter with the exact value.
4. Attendance/subjects come from attended_events (free-text event names). A subject counts as attended if an event name contains it.
5. Respect the dashboard filters already applied (they're applied automatically); mention them when they matter.
6. Reply in clear, friendly, concise English (or the user's language if they write in another). Lead with the direct answer in one sentence, then key figures as short bullets or a small markdown table (max ~15 rows; the full result table is shown to the user automatically below your answer). Format money as ₹ with Indian digit grouping (e.g. ₹1,25,000).
7. For follow-ups ("those", "them", "what about Andhra"), reuse the previous question's context.
8. End with exactly one line: FOLLOWUPS: <question 1> | <question 2> | <question 3> — three short, useful next questions the user could ask about this data."""


def llm_enabled():
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _client():
    import anthropic
    return anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], timeout=60)


def _chart_for(result):
    rows, cols = result.get("rows") or [], result.get("columns") or []
    if not rows or len(rows) > 25 or len(cols) < 2:
        return None
    label = cols[0]
    value = next((c for c in cols[1:] if all(isinstance(r.get(c), (int, float)) for r in rows)), None)
    if not value or not result.get("group"):
        return None
    return {"label": label, "value": value, "items": [{"label": str(r.get(label)), "value": r.get(value)} for r in rows]}


def run_tool(name, args, ui_filters):
    fn = TOOLS[name][0] if name in TOOLS else None
    if not fn:
        return {"error": f"Unknown tool {name}"}
    try:
        with student_data._connection() as conn, conn.cursor() as cur:
            return fn(cur, ui_filters, args or {})
    except ToolError as e:
        return {"error": str(e)}


def answer(question, history=None, ui_filters=None):
    """Run the tool-using agent loop. Returns the same shape as student_data.agent_answer plus extras."""
    question = (question or "").strip()
    if not question:
        raise ValueError("Ask a question about your student data.")
    if len(question) > 2000:
        raise ValueError("Please keep questions under 2000 characters.")
    client = _client()
    tools = [{"name": n, "description": d, "input_schema": s} for n, (_, d, s) in TOOLS.items()]
    messages = []
    for h in (history or [])[-8:]:
        role, content = h.get("role"), str(h.get("content") or "")[:4000]
        if role in ("user", "assistant") and content:
            if messages and messages[-1]["role"] == role:
                messages[-1]["content"] += "\n" + content
            else:
                messages.append({"role": role, "content": content})
    if messages and messages[0]["role"] == "assistant":
        messages.pop(0)
    if messages and messages[-1]["role"] == "user":
        messages.pop()
    active = {k: v for k, v in (ui_filters or {}).items() if v}
    import student_nlu
    hints = student_nlu.hints_for_llm(question, ui_filters)
    extra = (f"\n\n[Dashboard filters currently applied: {json.dumps(active)}]" if active else "") + (f"\n\n[{hints}]" if hints else "")
    messages.append({"role": "user", "content": question + extra})

    steps, last_result = [], None
    for _ in range(MAX_TOOL_ROUNDS):
        res = client.messages.create(model=MODEL, max_tokens=2000, system=SYSTEM, tools=tools, messages=messages)
        messages.append({"role": "assistant", "content": res.content})
        uses = [c for c in res.content if c.type == "tool_use"]
        if res.stop_reason != "tool_use" or not uses:
            text = "\n".join(c.text for c in res.content if c.type == "text").strip()
            return _finish(text, last_result, steps)
        results = []
        for u in uses:
            out = run_tool(u.name, u.input, ui_filters)
            steps.append({"tool": u.name, "input": u.input, "error": out.get("error")})
            if not out.get("error") and out.get("rows") and u.name != "find_values":
                last_result = {**out, "group": bool((u.input or {}).get("group_by")) or u.name == "event_breakdown"}
            payload = dict(out)
            if isinstance(payload.get("rows"), list) and len(payload["rows"]) > MAX_ROWS_TO_MODEL:
                payload["rows"] = payload["rows"][:MAX_ROWS_TO_MODEL]
                payload["note"] = f"Only first {MAX_ROWS_TO_MODEL} rows shown to you; the user sees the full table."
            results.append({"type": "tool_result", "tool_use_id": u.id, "content": json.dumps(payload, default=str)[:60000], "is_error": bool(out.get("error"))})
        messages.append({"role": "user", "content": results})
    return _finish("That question needed too many steps. Could you ask it in smaller parts?", last_result, steps)


def _finish(text, last_result, steps):
    followups = []
    m = re.search(r"^\s*FOLLOWUPS:\s*(.+)$", text, re.M)
    if m:
        followups = [q.strip() for q in m.group(1).split("|") if q.strip()][:3]
        text = text[: m.start()].rstrip()
    out = {"answer": text or "I couldn't find an answer in the data.", "format": "markdown", "followups": followups, "steps": steps, "mode": "ai",
           "rows": [], "columns": [], "chart": None}
    if last_result:
        out["rows"] = last_result.get("rows") or []
        out["columns"] = last_result.get("columns") or (list(out["rows"][0]) if out["rows"] else [])
        out["chart"] = _chart_for(last_result)
    return out
