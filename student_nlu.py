"""Question understanding for the Student AI Agent.

Works with or without an AI key:
  * Without a key it answers on its own (counts, breakdowns, months, fees, dues, lists, comparisons).
  * With a key it gives the AI model pre-resolved names ("GMC Kadapa" -> every spelling in the data).

Key idea: real Excel data spells the same college many ways ("Government Medical College, Kadapa",
"GMC Kadapa", "Govt. Medical College Kadappa"). Values are grouped into *entities* using abbreviation
expansion, initials (SMC = Siddhartha Medical College), place-name aliases and typo-tolerant matching,
so every question counts all spellings of the college the user meant.
"""
import calendar
import re
from datetime import date
from difflib import SequenceMatcher

import student_agent
import student_data

# ───────────── Text normalization ─────────────

EXPAND = {
    "gmc": ["government", "medical", "college"], "govt": ["government"], "gov": ["government"],
    "med": ["medical"], "coll": ["college"], "clg": ["college"], "mc": ["medical", "college"],
    "inst": ["institute"], "sci": ["sciences"], "univ": ["university"], "hosp": ["hospital"],
}
PLACE_ALIASES = {
    "cuddapah": "kadapa", "kadappa": "kadapa", "vizag": "visakhapatnam", "vishakapatnam": "visakhapatnam",
    "visakapatnam": "visakhapatnam", "bezawada": "vijayawada", "vja": "vijayawada", "hyd": "hyderabad",
    "secbad": "secunderabad", "nlr": "nellore", "tpt": "tirupati", "tirupathi": "tirupati",
    "rajahmundry": "rajamahendravaram", "kurnool": "kurnool", "guntur": "guntur",
}
GENERIC = {"government", "medical", "college", "institute", "of", "and", "the", "sciences", "science", "hospital",
           "research", "academy", "centre", "center", "university", "mbbs", "general", "teaching", "health", "dental",
           "&", "-", "s", "st", "dr", "sri", "shri"}
STOP = {"what", "is", "the", "count", "of", "for", "all", "in", "how", "many", "students", "student", "joined", "join",
        "from", "at", "admissions", "admission", "were", "was", "done", "did", "show", "me", "list", "give", "batch",
        "batches", "number", "total", "please", "and", "or", "with", "who", "which", "a", "an", "to", "by", "per", "each",
        "every", "month", "year", "do", "we", "have", "are", "there", "this", "that", "get", "find", "tell", "about",
        "between", "vs", "versus", "compare", "comparison", "wise", "only", "particular", "data", "enrolled", "enrollments",
        "taken", "happened", "made", "got", "so", "far", "till", "until", "now", "up", "on", "it", "our", "my", "college",
        "colleges", "medical", "government", "fee", "fees", "due", "dues", "paid", "pending", "amount", "collection"}


def _tokens(text):
    text = str(text or "").lower().replace("&", " and ")
    text = re.sub(r"[.'’]", "", text)  # "Govt." -> "govt", "Sec'bad" -> "secbad"
    return [PLACE_ALIASES.get(t, t) for t in re.findall(r"[a-z0-9]+", text)]


def _expand(tokens):
    out = []
    for t in tokens:
        out.extend(EXPAND.get(t, [t]))
    return out


def _initials(tokens):
    """Initialisms from runs of words: 'siddhartha medical college' -> {'smc', 'sm', 'mc', ...}."""
    words = [t for t in tokens if t not in {"of", "and", "the"} and not t.isdigit()]
    out = set()
    for i in range(len(words)):
        for j in range(i + 2, min(len(words), i + 5) + 1):
            out.add("".join(w[0] for w in words[i:j]))
    return out


def _similar(a, b):
    if a == b:
        return True
    if len(a) < 5 or len(b) < 5 or a[0] != b[0]:
        return False
    return SequenceMatcher(None, a, b).ratio() >= 0.84


# ───────────── Entity grouping ─────────────

class Entity:
    def __init__(self, field):
        self.field = field
        self.variants = {}   # stored value -> student count
        self.distinct = set()
        self.government = False
        self.initials = set()

    @property
    def label(self):
        # Most common spelling, preferring the longest among ties (usually the full official name).
        return max(self.variants.items(), key=lambda kv: (kv[1], len(kv[0])))[0]

    @property
    def count(self):
        return sum(self.variants.values())


def build_entities(field, rows):
    """rows: [(value, count)]. Groups spelling variants of the same name into one entity."""
    infos = []
    for value, n in rows:
        if not value:
            continue
        raw = _tokens(value)
        exp = _expand(raw)
        infos.append({"value": value, "n": n, "raw": raw, "distinct": {t for t in exp if t not in GENERIC and not t.isdigit()},
                      "gov": "government" in exp, "initials": _initials(exp) | {t for t in raw if t in EXPAND}})
    # Learn unknown initialisms (e.g. "smc") from other values' initials when they share a place/name token.
    all_initials = {}
    for inf in infos:
        for ini in inf["initials"]:
            all_initials.setdefault(ini, []).append(inf)
    for inf in infos:
        for t in list(inf["distinct"]):
            if 2 <= len(t) <= 5 and t in all_initials and t not in EXPAND:
                donors = [d for d in all_initials[t] if d is not inf and d["distinct"] & (inf["distinct"] - {t})]
                if not donors:
                    donors = [d for d in all_initials[t] if d is not inf]
                if len({frozenset(d["distinct"]) for d in donors}) == 1:
                    inf["distinct"] = (inf["distinct"] - {t}) | donors[0]["distinct"]
                    inf["gov"] = inf["gov"] or donors[0]["gov"]
    # Canonicalize typo variants of tokens across the field ("kadappa" ~ "kadapa").
    freq = {}
    for inf in infos:
        for t in inf["distinct"]:
            freq[t] = freq.get(t, 0) + inf["n"]
    canon = {}
    for t in sorted(freq, key=lambda x: -freq[x]):
        if t not in canon:
            for u in freq:
                if u not in canon and _similar(t, u):
                    canon[u] = t
    for inf in infos:
        inf["distinct"] = {canon.get(t, t) for t in inf["distinct"]}

    groups = {}
    for inf in infos:
        if not inf["distinct"]:
            key = ("__exact__", inf["value"].lower().strip())
        else:
            key = (frozenset(inf["distinct"]), inf["gov"])
        groups.setdefault(key, []).append(inf)
    # Merge a less specific name into the single more specific one ("Narayana Medical College" -> "..., Nellore").
    keys = list(groups)
    for k in keys:
        if k[0] == "__exact__" or k not in groups:
            continue
        supers = [o for o in groups if o != k and o[0] != "__exact__" and o[1] == k[1] and k[0] < o[0]]
        if len(supers) == 1:
            groups[supers[0]].extend(groups.pop(k))
    entities = []
    for k, infs in groups.items():
        e = Entity(field)
        for inf in infs:
            e.variants[inf["value"]] = e.variants.get(inf["value"], 0) + inf["n"]
            e.distinct |= inf["distinct"]
            e.initials |= inf["initials"]
            e.government = e.government or inf["gov"]
        entities.append(e)
    return entities


def _score(entity, qtokens, qexp):
    """How strongly the question mentions this entity. 0 = not mentioned."""
    if not entity.distinct:
        return 0.0
    matched = {d for d in entity.distinct if any(_similar(d, q) for q in qexp)}
    ini_hit = any(q in entity.initials and len(q) >= 3 for q in qtokens if q not in EXPAND)
    if not matched and not ini_hit:
        return 0.0
    score = 2.0 * len(matched) / len(entity.distinct) + len(matched) * 0.5 + (1.5 if ini_hit else 0)
    wants_gov = "government" in qexp
    if wants_gov and entity.government:
        score += 1.0
    elif wants_gov and not entity.government:
        score -= 1.5
    return score


def find_mentions(entities, question):
    """Return the entities the question mentions, best first, dropping weaker overlapping candidates."""
    qtokens = [PLACE_ALIASES.get(t, t) for t in _tokens(question)]
    qexp = _expand(qtokens)
    scored = [(s, e) for e in entities if (s := _score(e, qtokens, qexp)) > 0.9]
    scored.sort(key=lambda x: -x[0])
    specific = "government" in qexp or any(q in e.initials for _, e in scored for q in qtokens if len(q) >= 3 and q not in EXPAND)
    chosen, used, hit_sets = [], set(), []
    for s, e in scored:
        hits = frozenset(q for q in qexp if any(_similar(d, q) for d in e.distinct))
        if hits and hits <= used:
            # Same words already matched a stronger college. If the user only named a place shared by
            # several colleges (e.g. just "Kadapa"), keep them all; otherwise drop the weaker one.
            if specific or hits not in hit_sets or any(t in e.distinct for t in qexp if t not in hits and t not in STOP):
                continue
        chosen.append((s, e))
        used |= hits
        hit_sets.append(hits)
    return [e for _, e in chosen]


def unmatched_name_words(question, entities_by_field):
    """Words that look like a name the user mentioned but that matched nothing (for 'did you mean')."""
    known = set()
    for ents in entities_by_field.values():
        for e in ents:
            known |= e.distinct | e.initials
    words = [t for t in _tokens(question) if t not in STOP and not t.isdigit() and len(t) > 3 and t not in MONTHS and t not in GENERIC]
    return [w for w in words if not any(_similar(w, k) for k in known)]


# ───────────── Dates ─────────────

MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
MONTHS["sept"] = 9


def parse_period(question, today=None):
    """Returns (start, end_exclusive, label, assumed_year) or None. Based on joining date."""
    today = today or date.today()
    q = question.lower()

    def month_range(y, m):
        return date(y, m, 1), (date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1))

    if re.search(r"\bthis month\b", q):
        s, e = month_range(today.year, today.month); return s, e, s.strftime("%B %Y"), False
    if re.search(r"\blast month\b|\bprevious month\b", q):
        y, m = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
        s, e = month_range(y, m); return s, e, s.strftime("%B %Y"), False
    if re.search(r"\bthis year\b", q):
        return date(today.year, 1, 1), date(today.year + 1, 1, 1), str(today.year), False
    if re.search(r"\blast year\b", q):
        return date(today.year - 1, 1, 1), date(today.year, 1, 1), str(today.year - 1), False
    if re.search(r"\btoday\b", q):
        from datetime import timedelta
        return today, today + timedelta(days=1), "today", False

    found = [(m.start(), MONTHS[m.group(1)]) for m in re.finditer(r"\b(" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\b", q)]
    years = [int(y) for y in re.findall(r"\b(20\d{2})\b", q)]
    if found:
        months = [m for _, m in sorted(found)]
        year = years[0] if years else None
        assumed = year is None
        if year is None:  # most recent such month that is not in the future
            year = today.year if months[0] <= today.month else today.year - 1
        if len(months) >= 2 and re.search(r"\b(to|till|until|through|-|and|between)\b", q):
            y2 = years[1] if len(years) > 1 else (year if months[1] >= months[0] else year + 1)
            s, _ = month_range(year, months[0]); _, e = month_range(y2, months[1])
            return s, e, f"{s.strftime('%B %Y')} to {date(y2, months[1], 1).strftime('%B %Y')}", assumed
        s, e = month_range(year, months[0])
        return s, e, s.strftime("%B %Y"), assumed
    if years:
        y = years[0]
        return date(y, 1, 1), date(y + 1, 1, 1), str(y), False
    return None


# ───────────── Intent ─────────────

BREAKDOWNS = [
    ("batch", r"\b(all|each|every|per|by)\s+batch(es)?\b|\bbatch[\s-]*wise\b|\bacross\s+batch"),
    ("college", r"\b(each|every|per|by)\s+college|\bcollege[\s-]*wise\b|\ball\s+colleges\b"),
    ("state", r"\b(each|every|per|by)\s+state|\bstate[\s-]*wise\b|\ball\s+states\b"),
    ("joining_month", r"\bmonth[\s-]*wise\b|\bmonthly\b|\b(each|every|per|by)\s+month\b|\bmonth\s+by\s+month\b|\btrend\b"),
    ("employee", r"\b(each|every|per|by)\s+(employee|counsell?or|staff)|\b(employee|counsell?or|staff)[\s-]*wise\b"),
    ("mbbs_batch", r"\bmbbs[\s-]*(batch|year)[\s-]*wise\b|\b(each|every|per|by)\s+mbbs"),
    ("status", r"\bstatus[\s-]*wise\b|\b(each|every|per|by)\s+status\b"),
    ("payment_mode", r"\bpayment[\s-]*mode\b"),
    ("company", r"\bcompany[\s-]*wise\b|\b(each|every|per|by)\s+company\b"),
]


def parse_intent(question):
    q = question.lower()
    measure = "count"
    if re.search(r"\b(dues?|pending|balance|outstanding)\b", q):
        measure = "due"
    if re.search(r"\b(fees?|collection|collected|revenue|paid|payment)\b", q) and not re.search(r"payment[\s-]*mode", q):
        measure = "fees" if measure != "due" or re.search(r"\b(fees?|collection|collected|revenue)\b", q) else measure
    if re.search(r"\b(attend|attendance|class(es)?|events?)\b", q):
        measure = "attendance"
    listing = bool(re.search(r"\b(list|names?|who|details|show\s+(me\s+)?(the\s+)?(all\s+)?students|which\s+students|students\s+(from|in|of|at))\b", q)) and not re.search(r"\bhow\s+many\b|\bcount\b|\bnumber\s+of\b", q)
    breakdown = next((f for f, rx in BREAKDOWNS if re.search(rx, q)), None)
    top = re.search(r"\btop\s+(\d{1,3})\b", q)
    superlative = re.search(r"\b(most|highest|maximum|max|least|lowest|minimum|fewest)\b", q)
    if (superlative or top) and not breakdown:
        for f, word in (("college", r"college"), ("batch", r"batch"), ("state", r"state"), ("employee", r"employee|counsell?or|staff"), ("joining_month", r"month")):
            if re.search(r"\bwhich\s+(" + word + r")|\b(" + word + r")s?\s+(has|have|with|got|by)\b|\btop\s+\d+\s+(" + word + r")", q):
                breakdown = f
                break
    return {
        "measure": measure, "listing": listing, "breakdown": breakdown,
        "top": int(top.group(1)) if top else None,
        "ascending": bool(superlative and re.search(r"least|lowest|minimum|fewest", superlative.group(1))),
        "with_due": bool(re.search(r"\b(with|having|has|have)\s+(pending\s+)?(dues?|balance)|\bnot\s+(fully\s+)?paid\b|\bpending\s+(fees?|dues?|payment)\b", q)),
        "no_attendance": bool(re.search(r"\b(not|never|no|zero)\s+(attended|attending|attendance|class)", q)),
    }


# ───────────── Answering ─────────────

FIELDS_FOR_ENTITIES = {"college": "college", "batch": "batch_name", "state": "college_state", "employee": "employee_name",
                       "status": "status", "company": "company_options"}
LABEL = {"college": "college", "batch": "batch", "state": "state", "employee": "employee", "joining_month": "joining month",
         "mbbs_batch": "MBBS batch", "status": "status", "payment_mode": "payment mode", "company": "company"}


def _inr(v):
    v = int(round(float(v or 0)))
    s = str(abs(v))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        head = re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", head)
        s = f"{head},{tail}"
    return ("-₹" if v < 0 else "₹") + s


def load_entities(cursor, ui_filters):
    where, params = student_data._filtered_where(ui_filters)
    out = {}
    for key, col in FIELDS_FOR_ENTITIES.items():
        cursor.execute(f"SELECT {col}, COUNT(*) FROM student_records {where} AND {col} IS NOT NULL AND {col} <> '' GROUP BY 1", params)
        rows = cursor.fetchall()
        if key in ("college",):
            out[key] = build_entities(key, rows)
        else:  # short categorical fields: exact value, plus compact-text matching
            ents = []
            for v, n in rows:
                e = Entity(key); e.variants[v] = n
                toks = _tokens(v); e.distinct = {t for t in toks if t not in GENERIC} or set(toks); e.initials = _initials(toks)
                ents.append(e)
            out[key] = ents
    return out


def _batch_mentions(entities, question):
    """Batches are code-like ('SSP2 (B)'): match on compact text, e.g. 'ssp2b' or 'ssp 2 b'."""
    compact_q = re.sub(r"[^a-z0-9]", "", question.lower())
    words = re.findall(r"[a-z0-9]+", question.lower())
    grams = {"".join(words[i:j]) for i in range(len(words)) for j in range(i + 1, min(len(words), i + 4) + 1)}
    hits = []
    for e in entities:
        for v in e.variants:
            cv = re.sub(r"[^a-z0-9]", "", v.lower())
            head = re.sub(r"[^a-z0-9]", "", re.split(r"[\s(]", v.lower().strip())[0] + "".join(re.findall(r"\(([a-z0-9])\)", v.lower())))
            if (len(cv) >= 4 and cv in compact_q) or (len(head) >= 4 and head in grams):
                hits.append(e)
                break
    if not hits:
        hits = [e for e in entities if e.distinct and all(any(_similar(d, t) for t in _tokens(question)) for d in e.distinct if len(d) > 3) and any(len(d) > 3 for d in e.distinct)]
    return hits


def understand(question, ui_filters=None):
    """Parse a question into filters/grouping against the real data. Returns a plan dict."""
    with student_data._connection() as conn, conn.cursor() as cur:
        ents = load_entities(cur, ui_filters)
    plan = {"filters": [], "mentions": {}, "period": parse_period(question), "intent": parse_intent(question)}
    q = question
    colleges = find_mentions(ents["college"], q)
    parts = [p for p in re.split(r"\b(?:vs|versus|and|compare(?:d)?(?:\s+to)?|against|or)\b|,|&|/", q, flags=re.I) if p.strip()]
    if len(parts) > 1:  # comparisons: read each part on its own ("GMC Kadapa vs Siddhartha vs Narayana")
        seen = {id(e) for e in colleges}
        for part in parts:
            for e in find_mentions(ents["college"], part):
                if id(e) not in seen:
                    colleges.append(e); seen.add(id(e))
    if colleges:
        plan["mentions"]["college"] = colleges
    states = [e for e in ents["state"] if any(_similar(d, t) for d in e.distinct for t in _tokens(q) if len(d) > 3)]
    sl = q.lower()
    for abbr, name in (("ap", "andhra"), ("ts", "telangana"), ("tg", "telangana"), ("tn", "tamil"), ("ka", "karnataka")):
        if re.search(r"\b" + abbr + r"\b", sl):
            states += [e for e in ents["state"] if name in e.label.lower()]
    if states:
        plan["mentions"]["state"] = list({id(e): e for e in states}.values())
    batches = _batch_mentions(ents["batch"], q)
    if batches:
        plan["mentions"]["batch"] = batches
    measure_words = {"pending", "paid", "due", "dues", "active", "completed", "fees", "fee"}
    for key in ("employee", "status", "company"):
        if key == "status" and not re.search(r"\bstatus\b", sl):
            continue
        hits = [e for e in ents[key] if e.distinct and not (e.distinct <= measure_words and key != "status")
                and all(any(_similar(d, t) for t in _tokens(q)) for d in e.distinct)]
        if hits:
            plan["mentions"][key] = hits
    plan["unknown_words"] = unmatched_name_words(q, ents) if not plan["mentions"] else []
    plan["entities"] = ents
    return plan


def _base_filters(plan, exclude=()):
    fl = []
    for key, es in plan["mentions"].items():
        if key in exclude:
            continue
        vals = [v for e in es for v in e.variants]
        fl.append({"field": key, "op": "in", "value": vals})
    if plan["period"]:
        s, e, _, _ = plan["period"]
        fl.append({"field": "joining_date", "op": "gte", "value": s.isoformat()})
        fl.append({"field": "joining_date", "op": "lt", "value": e.isoformat()})
    it = plan["intent"]
    if it["with_due"] or (it["measure"] == "due" and it["listing"]):
        fl.append({"field": "due", "op": "gt", "value": 0})
    if it["no_attendance"]:
        fl.append({"field": "events_attended", "op": "eq", "value": 0})
    return fl


def _run(args, ui_filters):
    out = student_agent.run_tool("query_students", args, ui_filters)
    if out.get("error"):
        raise RuntimeError(out["error"])
    return out


def _metrics(measure):
    if measure == "fees":
        return [{"fn": "count"}, {"fn": "sum", "field": "total_fee"}, {"fn": "sum", "field": "paid"}, {"fn": "sum", "field": "due"}]
    if measure == "due":
        return [{"fn": "count"}, {"fn": "sum", "field": "due"}]
    if measure == "attendance":
        return [{"fn": "count"}, {"fn": "sum", "field": "events_attended"}]
    return [{"fn": "count"}]


def _describe(plan):
    parts = []
    m = plan["mentions"]
    if "college" in m:
        parts.append("from " + " / ".join(f"**{e.label}**" for e in m["college"]))
    if "batch" in m:
        parts.append("in " + ", ".join(f"**{e.label}**" for e in m["batch"]))
    if "state" in m:
        parts.append("from " + ", ".join(f"**{e.label}**" for e in m["state"]))
    for k in ("employee", "status", "company"):
        if k in m:
            parts.append(f"with {LABEL[k]} " + ", ".join(f"**{e.label}**" for e in m[k]))
    if plan["period"]:
        parts.append(f"who joined in **{plan['period'][2]}**")
    return " ".join(parts)


def _variant_note(plan):
    notes = []
    for e in plan["mentions"].get("college", []):
        others = [v for v in e.variants if v != e.label]
        if others:
            notes.append(f"For **{e.label}** I also counted the other spellings in your data: " + ", ".join(f"“{v}” ({e.variants[v]})" for v in others) + ".")
    return " ".join(notes)


def answer(question, ui_filters=None):
    """Deterministic answer. Returns None if the question isn't understood (caller falls back)."""
    plan = understand(question, ui_filters)
    it, m = plan["intent"], plan["mentions"]
    if not m and not plan["period"] and not it["breakdown"] and not it["with_due"] and not it["no_attendance"]:
        if plan["unknown_words"]:
            return _not_found(plan, question)
        return None

    multi = [k for k, v in m.items() if len(v) > 1 and k in ("college", "batch", "state")]
    group = it["breakdown"]
    if not group and multi:
        group = multi[0]  # "GMC Kadapa vs Siddhartha" -> one row per college
    desc = _describe(plan)
    note = _variant_note(plan)
    period_note = " (based on joining date" + (", assuming the most recent " + plan["period"][2].split()[0] if plan["period"] and plan["period"][3] else "") + ")" if plan["period"] else ""

    filters = _base_filters(plan)
    total = _run({"filters": filters, "limit": 1}, ui_filters)["matching_students"]

    if it["listing"] and not group:
        cols = ["name", "mobile", "batch", "college", "state", "joining_date", "paid", "due", "events_attended"]
        res = _run({"filters": filters, "columns": cols, "order_by": "due" if it["measure"] == "due" else "name", "limit": 500}, ui_filters)
        text = f"I found **{total} student{'s' if total != 1 else ''}** {desc}{period_note}." + (f" Showing all of them below." if total else "")
        return _result(text, note, res["rows"], cols, None, plan, question)

    if group:
        rows = _grouped(plan, group, filters, it, ui_filters)
        cols = list(rows[0].keys()) if rows else [LABEL[group], "students"]
        chart = {"label": cols[0], "value": cols[1], "items": [{"label": str(r[cols[0]]), "value": r[cols[1]]} for r in rows[:25]]} if 1 < len(rows) <= 25 else None
        if total == 0:
            text = f"No students {desc}{period_note} were found."
        elif plan["intent"]["top"] or re.search(r"\b(most|highest|maximum|least|lowest|fewest)\b", question.lower()):
            best = rows[0]
            text = f"**{best[cols[0]]}** has the {'fewest' if it['ascending'] else 'most'} — **{_fmt(cols[1], best[cols[1]])}** {cols[1]}{(' ' + desc) if desc else ''}{period_note}."
        else:
            text = f"**{total} student{'s' if total != 1 else ''}** {desc or 'in total'}{period_note}. Here is the {LABEL[group]}-wise breakdown ({len(rows)} {_plural(LABEL[group], len(rows))}):"
        if not chart:  # a single row: say it in words; otherwise the chart/table below shows it
            text += "\n\n" + _md_table(rows[:15], cols) if len(rows) > 1 else ""
        return _result(text, note, rows, cols, chart, plan, question)

    # Single total (with money if asked)
    if it["measure"] in ("fees", "due", "attendance"):
        agg = _aggregate(filters, ui_filters)
        if it["measure"] == "fees":
            text = f"**{total} students** {desc}{period_note}: total fee **{_inr(agg['total_fee'])}**, paid **{_inr(agg['paid'])}**, due **{_inr(agg['due'])}**."
        elif it["measure"] == "due":
            text = f"**{agg['with_due']} of {total} students** {desc}{period_note} have dues, totalling **{_inr(agg['due'])}**."
        else:
            text = f"**{agg['with_att']} of {total} students** {desc}{period_note} have attended at least one class ({agg['events']} class attendances in total)."
        return _result(text, note, [], [], None, plan, question)
    verb = "joined" if plan["period"] or re.search(r"join|admission|enrol", question.lower()) else "are"
    if total == 0:
        text = f"I couldn't find any students {desc}{period_note}."
        if plan["period"] and m:
            other = _run({"filters": _base_filters({**plan, "period": None}), "group_by": ["joining_month"], "metrics": [{"fn": "count"}], "order_by": "joining_month", "order": "asc", "limit": 60}, ui_filters)
            if other["matching_students"]:
                months = ", ".join(f"{_month_name(r['joining_month'])} ({r['students']})" for r in other["rows"] if r["joining_month"] != "(blank)")
                text += f"\n\n{_describe({**plan, 'period': None}).replace('from ', 'Students from ', 1)} joined in these months instead: {months}."
    else:
        text = f"**{total} student{'s' if total != 1 else ''}** {verb} {desc}{period_note}." if verb == "joined" else f"There {'is' if total == 1 else 'are'} **{total} student{'s' if total != 1 else ''}** {desc}{period_note}."
        if verb == "joined":
            text = text.replace(" who joined in ", " in ")
    cols = ["name", "mobile", "batch", "college", "joining_date", "paid", "due"]
    res = _run({"filters": filters, "columns": cols, "limit": 500}, ui_filters) if total else {"rows": []}
    return _result(text, note, res["rows"], cols, None, plan, question)


def _month_name(ym):
    try:
        y, m = ym.split("-")
        return f"{calendar.month_abbr[int(m)]} {y}"
    except Exception:
        return ym


def _plural(word, n):
    if n == 1:
        return word
    return word + ("es" if word.endswith(("ch", "s", "sh", "x")) else "s")


def _fmt(col, v):
    return _inr(v) if re.search(r"fee|paid|due", col) else (f"{v:,}" if isinstance(v, (int, float)) else str(v))


def _aggregate(filters, ui_filters):
    where, params = student_agent._scoped_where(ui_filters, filters)
    with student_data._connection() as conn, conn.cursor() as cur:
        cur.execute(f"""SELECT COALESCE(SUM(total_fee),0), COALESCE(SUM(paid_amount),0), COALESCE(SUM(due_amount),0),
            COUNT(*) FILTER (WHERE COALESCE(due_amount,0) > 0), COUNT(*) FILTER (WHERE COALESCE(attended_event_count,0) > 0),
            COALESCE(SUM(attended_event_count),0) FROM student_records {where}""", params)
        r = cur.fetchone()
    return {"total_fee": float(r[0]), "paid": float(r[1]), "due": float(r[2]), "with_due": r[3], "with_att": r[4], "events": int(r[5])}


def _grouped(plan, group, filters, it, ui_filters):
    """Group rows. College grouping merges spelling variants into one row per real college."""
    metrics = _metrics(it["measure"])
    label = LABEL[group].capitalize()
    if group == "college":
        res = _run({"filters": filters, "group_by": ["college"], "metrics": metrics, "limit": 500}, ui_filters)
        ents = plan["mentions"].get("college") or plan["entities"]["college"]
        index = {v: e for e in ents for v in e.variants}
        merged = {}
        for r in res["rows"]:
            e = index.get(r["college"])
            key = e.label if e else r["college"]
            acc = merged.setdefault(key, {label: key})
            for k, v in r.items():
                if k != "college" and isinstance(v, (int, float)):
                    acc[k] = acc.get(k, 0) + v
        rows = list(merged.values())
    else:
        res = _run({"filters": filters, "group_by": [group], "metrics": metrics, "limit": 500}, ui_filters)
        rows = [{label: (_month_name(r[group]) if group == "joining_month" else r[group]), "_sort": r[group], **{k: v for k, v in r.items() if k != group}} for r in res["rows"]]
    names = {"students": "students", "sum_total_fee": "total fee", "sum_paid": "paid", "sum_due": "due", "sum_events_attended": "classes attended"}
    rows = [{(names.get(k, k)): v for k, v in r.items()} for r in rows]
    sort_key = "due" if it["measure"] == "due" else "students"
    if group == "joining_month" and not it["top"]:
        rows.sort(key=lambda r: str(r.get("_sort", r[label])))
    else:
        rows.sort(key=lambda r: (r.get(sort_key) or 0), reverse=not it["ascending"])
    if it["top"]:
        rows = rows[: it["top"]]
    for r in rows:
        r.pop("_sort", None)
    return rows


def _md_table(rows, cols):
    if not rows:
        return ""
    head = "| " + " | ".join(c.capitalize() for c in cols) + " |\n|" + "---|" * len(cols)
    body = "\n".join("| " + " | ".join(_fmt(c, r.get(c)) for c in cols) + " |" for r in rows)
    return head + "\n" + body


def _not_found(plan, question):
    words = plan["unknown_words"]
    names = sorted({e.label for e in plan["entities"]["college"]}, key=lambda n: -max(SequenceMatcher(None, w, n.lower()).ratio() for w in words))
    text = f"I couldn't find **{' '.join(words)}** in your data's colleges, batches or states."
    if names:
        text += " Did you mean one of these?\n\n" + "\n".join(f"- {n}" for n in names[:5])
    return {"answer": text, "rows": [], "columns": [], "chart": None, "mode": "builtin", "format": "markdown",
            "followups": [f"How many students joined from {n}?" for n in names[:3]], "steps": []}


def _result(text, note, rows, cols, chart, plan, question):
    if note:
        text += "\n\nℹ️ " + note
    follow = []
    m = plan["mentions"]
    if "college" in m and len(m["college"]) == 1:
        c = m["college"][0].label
        if plan["intent"]["breakdown"] != "batch":
            follow.append(f"{c} students batch-wise")
        follow.append(f"Month-wise admissions from {c}")
        follow.append(f"Pending dues of {c} students")
    elif plan["period"]:
        follow += [f"College-wise admissions in {plan['period'][2]}", f"Batch-wise admissions in {plan['period'][2]}"]
    else:
        follow += ["Top 10 colleges by students", "Month-wise admissions this year"]
    return {"answer": text, "rows": rows, "columns": cols, "chart": chart, "mode": "builtin", "format": "markdown",
            "followups": follow[:3], "steps": [{"tool": "understood: " + ", ".join(
                [f"{k} = {', '.join(e.label for e in v)}" for k, v in m.items()] + ([f"period = {plan['period'][2]}"] if plan["period"] else []) +
                ([f"breakdown by {plan['intent']['breakdown']}"] if plan["intent"]["breakdown"] else []))}]}


def hints_for_llm(question, ui_filters=None):
    """Resolved names to give the AI model, so it filters with every spelling variant."""
    try:
        plan = understand(question, ui_filters)
    except Exception:
        return ""
    lines = []
    for k, es in plan["mentions"].items():
        for e in es:
            lines.append(f"- {k} '{e.label}': use op 'in' with ALL these stored values: {list(e.variants)}")
    if plan["period"]:
        s, e, label, assumed = plan["period"]
        lines.append(f"- period: {label} → joining_date >= {s} and < {e}" + (" (year assumed: most recent)" if assumed else ""))
    if plan.get("unknown_words"):
        lines.append(f"- words not found in college/batch/state names: {plan['unknown_words']}")
    return ("Pre-resolved from the data (trust these):\n" + "\n".join(lines)) if lines else ""


FOLLOWUP_RE = re.compile(r"^\s*(what about|how about|and|same for|also|now|then|ok(ay)?|for)\b|^\s*(those|them|that|these)\b", re.I)


def with_context(question, previous):
    """Short follow-ups ("what about Siddhartha?", "and in October?") inherit the previous question's
    breakdown / time period / measure words, but use the new names the user mentions."""
    if not previous or not FOLLOWUP_RE.search(question) or len(question.split()) > 12:
        return question
    carry = []
    pq = previous.lower()
    for _, rx in BREAKDOWNS:
        m = re.search(rx, pq)
        if m and not re.search(rx, question.lower()):
            carry.append(m.group(0))
    if parse_period(previous) and not parse_period(question):
        carry += re.findall(r"\b(?:" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r"|20\d\d|this month|last month|this year|last year)\b", pq)
    for w in ("dues", "fee collection", "list", "attendance"):
        if w in pq and w not in question.lower():
            carry.append(w)
    q_has_names = any(t not in STOP and t not in MONTHS and t not in GENERIC and not t.isdigit() and len(t) > 2
                      and t not in {"about", "same", "also", "then", "okay", "those", "them", "these", "wise", "month", "year", "last", "next"}
                      for t in _tokens(FOLLOWUP_RE.sub("", question)))
    if not q_has_names:  # "and in October?" -> keep the previous names too
        if parse_period(question):  # the new period replaces the old one
            previous = re.sub(r"\b(?:" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r"|20\d\d|this month|last month|this year|last year|today)\b", " ", previous, flags=re.I)
        return previous + " " + question
    return (question + " " + " ".join(carry)).strip()
