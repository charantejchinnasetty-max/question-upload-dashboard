"""DOCX-to-question parser.  The emitted shape matches the project's existing JSON files."""
import html
import re
import uuid
from datetime import datetime, timezone

from docx import Document


QUESTION_RE = re.compile(r"^Question\s*\d*\s*:\s*", re.IGNORECASE)
ANSWER_RE = re.compile(r"^Answer\s*:\s*([A-Z])", re.IGNORECASE)
EXPLANATION_RE = re.compile(r"^Explanation\s*:\s*", re.IGNORECASE)
OPTION_RE = re.compile(r"^([A-Z])[.)]\s*(.*)$")
BULLET_RE = re.compile(r"^[•·\-▪◦]\s*")


def _lines(paragraph):
    """Return raw/HTML lines, retaining bold runs from a Word paragraph."""
    raw_lines = paragraph.text.replace("\xa0", " ").split("\n")
    rendered, current = [], ""
    for run in paragraph.runs:
        for index, part in enumerate(run.text.replace("\xa0", " ").split("\n")):
            value = html.escape(part)
            if run.bold and value.strip():
                value = f"<strong>{value}</strong>"
            current += value
            if index < len(run.text.replace("\xa0", " ").split("\n")) - 1:
                rendered.append(current.strip())
                current = ""
    if current.strip() or not rendered:
        rendered.append(current.strip())
    size = max(len(raw_lines), len(rendered))
    raw_lines.extend([""] * (size - len(raw_lines)))
    rendered.extend([""] * (size - len(rendered)))
    style = paragraph.style.name.lower() if paragraph.style else ""
    return [
        {"raw": raw.strip(), "html": rendered[i].strip(),
         "bullet": "bullet" in style or "list" in style or bool(BULLET_RE.match(raw.strip()))}
        for i, raw in enumerate(raw_lines) if raw.strip()
    ]


def _items(doc):
    # Mirrors the established parser: body paragraphs followed by table-cell content.
    result = []
    for paragraph in doc.paragraphs:
        result.extend(_lines(paragraph))
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for paragraph in cell.paragraphs:
                    result.extend(_lines(paragraph))
    return result


def _description(lines):
    parts, in_list = [], False
    for item in lines:
        raw, value = item["raw"], BULLET_RE.sub("", item["html"]).strip()
        if ":" in value and "<strong>" not in value:
            lead, tail = value.split(":", 1)
            value = f"<strong>{lead.strip()}:</strong> {tail.strip()}"
        bullet = item["bullet"] or (":" in raw and not raw.lower().startswith(("question", "answer", "explanation")))
        if bullet:
            if not in_list:
                parts.append("<ul>"); in_list = True
            parts.append(f"<li>{value}</li>")
        else:
            if in_list:
                parts.append("</ul>"); in_list = False
            parts.append(f"<p>{value}</p>")
    if in_list:
        parts.append("</ul>")
    return "\n".join(parts)


def parse_docx(file_or_path, subject_id, created_by=None):
    """Parse a DOCX into (questions, errors) without changing question schema."""
    entries, questions, errors = _items(Document(file_or_path)), [], []
    index, order = 0, 1
    created_by = created_by or {"uuid": "290604b4-498f-4428-8e4c-92cb9f3240d0", "name": "Akhil"}
    while index < len(entries):
        line = entries[index]["raw"]
        if not QUESTION_RE.match(line):
            index += 1; continue
        title = QUESTION_RE.sub("", line).strip()
        raw_block, options, option_map = [line], [], {}
        index += 1
        while index < len(entries):
            current = entries[index]["raw"]
            raw_block.append(current)
            if ANSWER_RE.match(current):
                break
            match = OPTION_RE.match(current)
            if not match:
                break
            letter, option_text = match.groups()
            option_map[letter] = len(options) + 1
            options.append({"name": f"<p>{html.escape(option_text)}</p>", "value": len(options) + 1, "imgUrl": "", "upload": False})
            index += 1
        answer, answer_letter = None, None
        if index < len(entries):
            match = ANSWER_RE.match(entries[index]["raw"])
            if match:
                answer_letter = match.group(1).upper(); answer = option_map.get(answer_letter); index += 1
        if answer is None:
            errors.append({"question_number": order, "question": title, "reason": f"Answer '{answer_letter or 'missing'}' is not present in the options", "missing_field": "Answer", "recommended_correction": "Add an Answer: line using one of the available option letters.", "raw_block": raw_block})
            while index < len(entries) and not QUESTION_RE.match(entries[index]["raw"]): index += 1
            continue
        explanation = []
        if index < len(entries) and EXPLANATION_RE.match(entries[index]["raw"]):
            first = EXPLANATION_RE.sub("", entries[index]["raw"]).strip()
            first_html = re.sub(r"^Explanation\s*:\s*", "", entries[index]["html"], flags=re.I).strip()
            if first: explanation.append({"raw": first, "html": first_html, "bullet": False})
            index += 1
            while index < len(entries) and not QUESTION_RE.match(entries[index]["raw"]):
                explanation.append(entries[index]); index += 1
        questions.append({"uuid": str(uuid.uuid4()), "Question": f"<p>{html.escape(title)}</p>", "type": "SINGLE", "options": options, "Answer": {"options": answer}, "Question_image": "", "description": _description(explanation), "difficulty": "EASY", "previous_apperance": "", "perals": [], "tags": None, "questionId": "", "flags": {"pro": False, "editable": True, "qBank": True, "active": True, "testSeries": True, "paid": True}, "testSeries": None, "Question_order": order, "syllabus": [subject_id] if subject_id else [], "math_library": "no", "description_image": "", "createdOn": datetime.now(timezone.utc).isoformat(), "createdBy": created_by, "modifiedOn": None})
        order += 1
    return questions, errors
