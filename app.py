import json
import io
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, send_file
from openpyxl import load_workbook
from pymongo import MongoClient
from werkzeug.utils import secure_filename

from parser import create_standard_docx, parse_docx
import attendance_reports
import student_data
<<<<<<< HEAD
import student_agent
import student_nlu
import auth
=======
import s3_images
>>>>>>> cb419b7 (add DOCX image processing and S3 JSON export)

load_dotenv()
BASE_DIR = Path(__file__).resolve().parent
# Vercel's deployed application directory is read-only; /tmp is writable during a request.
RUNTIME_DIR = Path(tempfile.gettempdir()) / "question-upload-dashboard" if os.environ.get("VERCEL") else BASE_DIR
UPLOAD_DIR, GENERATED_DIR = RUNTIME_DIR / "uploads", RUNTIME_DIR / "generated"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True); GENERATED_DIR.mkdir(parents=True, exist_ok=True)
MASTER_TEMPLATE = BASE_DIR / "static" / "samples" / "Question_Format_Sample.docx"
HISTORY_FILE, ERROR_FILE = GENERATED_DIR / "upload_history.json", GENERATED_DIR / "error_questions.json"
LOGIN_URL = "https://admin.errorfreetestseries.in/managebe/proxy/api/employee/employeeLogin"
UPLOAD_URL = "https://admin.errorfreetestseries.in/managebe/proxy/api/questions/bulk/testQuestions"
app = Flask(__name__); app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024; 
auth.init_auth(app)
_jobs = {}; _client = None

def _db():
    global _client
    uri = os.environ.get("MONGO_URI")
    if not uri: raise RuntimeError("MONGO_URI is not configured. Add it to .env to load categories.")
    if _client is None: _client = MongoClient(uri, serverSelectionTimeoutMS=5000)
    return _client["e_drona"]

def _read(path, default):
    try:
        with path.open(encoding="utf-8") as f: return json.load(f)
    except (OSError, json.JSONDecodeError): return default

def _write(path, value):
    with path.open("w", encoding="utf-8") as f: json.dump(value, f, indent=2, ensure_ascii=False)

def _valid_docx(file): return bool(file and file.filename and file.filename.lower().endswith(".docx"))

def login():
    mobile, password = os.environ.get("API_MOBILE"), os.environ.get("API_PASSWORD")
    if not mobile or not password: raise RuntimeError("API_MOBILE and API_PASSWORD must be configured in .env before uploading.")
    response = requests.post(LOGIN_URL, json={"mobile": mobile, "password": password}, timeout=30); response.raise_for_status()
    try: return response.json()["response"]["accessToken"]
    except (KeyError, TypeError, ValueError) as exc: raise RuntimeError("Login API did not return an access token.") from exc

def send_questions(questions, ts_uuid, test_uuid):
    payload = {"ts_uuid": ts_uuid, "test": test_uuid, "que": questions, "test_que": [{"uuid": q["uuid"], "order": i + 1, "positive": 1, "negative": 0} for i, q in enumerate(questions)], "date": datetime.now(timezone.utc).isoformat()}
    response = requests.post(UPLOAD_URL, json=payload, headers={"Authorization": f"Bearer {login()}", "Content-Type": "application/json"}, timeout=90); response.raise_for_status()
    try: return response.json()
    except ValueError: return {"message": response.text}

@app.get("/")
def dashboard(): return render_template("dashboard.html")
@app.get("/upload-questions")
def upload_page(): return render_template("upload.html")
@app.get("/history")
def history_page(): return render_template("history.html")
@app.get("/error-questions")
def errors_page(): return render_template("errors.html")
@app.get("/attendance-reports")
def attendance_reports_page(): return render_template("attendance_reports.html")
@app.get("/book-event-json")
def book_event_json_page(): return render_template("book_event_json.html")
@app.get("/student-dashboard")
def student_dashboard_page(): return render_template("student_dashboard.html")

def _apply_image_urls(questions, uploaded):
    """Place verified S3 URLs into the matching company JSON fields."""
    by_question = {}
    for image in uploaded:
        by_question.setdefault(int(image["question_number"]), []).append(image)
    for question in questions:
        images = by_question.get(int(question.get("Question_order", 0)), [])
        for image in images:
            url = image["url"]
            section = image.get("section")
            if section == "OPTION" and image.get("option_label"):
                value = ord(image["option_label"]) - 64
                for option in question.get("options", []):
                    if option.get("value") == value:
                        option["imgUrl"], option["upload"] = url, True
                        break
            elif section == "QUESTION":
                question["Question_image"] = question.get("Question_image") or url
                question["Question"] += f'<p><img src="{url}" alt="Question image"></p>'
            else:
                question["description_image"] = url if not question.get("description_image") else question["description_image"]
                question["description"] = (question.get("description") or "") + f'<p><img src="{url}" alt="Explanation image"></p>'
    return questions

@app.post("/api/process-s3/<job_id>")
def process_s3(job_id):
    job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": "Processing job expired. Analyze the DOCX again."}), 404
    records = [record for candidate in job.get("candidates", []) for record in candidate.get("image_records", []) if record.get("path")]
    if not records:
        return jsonify({"error": "No embedded images were found in this document."}), 400
    try:
        target_kb = int(request.form.get("target_kb", "200"))
        if target_kb not in {100, 200, 300, 500}:
            raise ValueError("Image target must be 100, 200, 300, or 500 KB.")
        uploaded = s3_images.upload_images(records, request.form.get("subject") or job.get("subject") or "QUESTIONS", target_kb)
        questions = _apply_image_urls(job["questions"], uploaded)
        _write(job["output_path"], questions)
        job["questions"] = questions
        return jsonify({"status": "ready", "uploaded": len(uploaded), "images": uploaded, "download_url": f"/download/{job_id}"})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": f"S3 image processing failed: {exc}"}), 502

@app.get("/student-ai-agent")
def student_ai_agent_page(): return render_template("student_ai_agent.html")

@app.get("/api/students/agent/status")
def student_ai_agent_status():
    prov = student_agent.provider(request.headers.get("x-vercel-oidc-token"))
    return jsonify({"mode": "ai" if prov else "builtin", "provider": prov["label"] if prov else None, "model": prov["model"] if prov else None})

@app.post("/api/students/agent")
def student_ai_agent():
    payload = request.get_json(silent=True) or {}
    question = str(payload.get("question", "")).strip()
    history = payload.get("history") or []
    if question.lower().startswith(("what about", "and ", "those", "them", "that ")):
        previous = next((str(item.get("content", "")) for item in reversed(history) if item.get("role") == "user"), "")
        if previous:
            question = f"{previous}. Follow-up: {question}"
    filters = payload.get("filters") or {}
    oidc = request.headers.get("x-vercel-oidc-token")
    ai_error = None
    if student_agent.llm_enabled(oidc):
        try:
            return jsonify(student_agent.answer(str(payload.get("question", "")), history, filters, oidc))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:  # AI provider down / quota / bad key: fall back to the built-in agent
            app.logger.warning("AI agent failed, using built-in agent: %s", exc)
            ai_error = str(exc)[:200]
    try:
        try:
            raw = str(payload.get("question", "")).strip()
            prev = next((str(h.get("content", "")) for h in reversed(history) if h.get("role") == "user"), "")
            result = student_nlu.answer(student_nlu.with_context(raw, prev), filters)
        except Exception as exc:  # never let the smart parser break the agent
            app.logger.warning("Question parser failed, using keyword agent: %s", exc)
            result = None
        if result is None:
            result = student_data.agent_answer(question, filters)
        result.setdefault("mode", "builtin")
        if ai_error:
            result["ai_error"] = ai_error
        return jsonify(result)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 503
    except Exception:
        return jsonify({"error": "The Student AI Agent could not query the active student dataset."}), 500

@app.post("/api/students/import")
def student_import():
    file = request.files.get("file")
    if not file or not file.filename or not file.filename.lower().endswith(".xlsx"):
        return jsonify({"error": "Choose a valid student .xlsx file."}), 400
    try:
        payload = file.read(25 * 1024 * 1024 + 1)
        if len(payload) > 25 * 1024 * 1024:
            return jsonify({"error": "The Excel file exceeds the 25 MB limit."}), 413
        result = student_data.import_workbook(io.BytesIO(payload), secure_filename(file.filename), request.form.get("mode") == "replace")
        return jsonify(result), 201
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 503
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        return jsonify({"error": "The student import could not be completed. Check the Excel structure and database connection."}), 500

@app.get("/api/students/dashboard")
def student_dashboard_data():
    try: return jsonify(student_data.dashboard_data(request.args))
    except RuntimeError as exc: return jsonify({"error": str(exc)}), 503
    except Exception: return jsonify({"error": "Could not load the student dashboard. Confirm DATABASE_URL and import a student Excel file."}), 500

@app.get("/api/students/records")
def student_records_data():
    try: return jsonify({"records": student_data.student_records(request.args)})
    except RuntimeError as exc: return jsonify({"error": str(exc)}), 503
    except Exception: return jsonify({"error": "Could not load matching students."}), 500

@app.get("/api/students/colleges")
def student_college_counts():
    try: return jsonify({"colleges": student_data.college_counts(request.args)})
    except RuntimeError as exc: return jsonify({"error": str(exc)}), 503
    except Exception: return jsonify({"error": "Could not load college counts."}), 500

@app.post("/api/book-event-json/convert")
def convert_book_event_json():
    file = request.files.get("file")
    if not file or not file.filename or not file.filename.lower().endswith(".xlsx"):
        return jsonify({"error": "Choose a valid .xlsx file containing a mobile column."}), 400
    payload = file.read(20 * 1024 * 1024 + 1)
    if len(payload) > 20 * 1024 * 1024:
        return jsonify({"error": "The Excel file exceeds the 20 MB limit."}), 413
    try:
        worksheet = load_workbook(io.BytesIO(payload), read_only=True, data_only=True).active
        rows = worksheet.iter_rows(values_only=True)
        headers = next(rows, None)
        if not headers:
            raise ValueError("The Excel file is empty.")
        mobile_index = next((index for index, header in enumerate(headers) if str(header or "").strip().lower() == "mobile"), None)
        if mobile_index is None:
            raise ValueError("The Excel file needs a column named mobile.")
        records = []
        for row in rows:
            value = row[mobile_index] if mobile_index < len(row) else None
            if value is None or str(value).strip() == "":
                continue
            mobile = str(value).strip()
            if mobile.endswith(".0") and mobile[:-2].isdigit():
                mobile = mobile[:-2]
            records.append({"mobile": mobile})
        if not records:
            raise ValueError("No mobile numbers were found in the Excel file.")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": f"Could not read the Excel file: {exc}"}), 400
    output_name = f"{Path(secure_filename(file.filename)).stem}.json"
    return send_file(io.BytesIO(json.dumps(records, ensure_ascii=False, indent=2).encode("utf-8")), as_attachment=True, download_name=output_name, mimetype="application/json")

def _attendance_dataframe():
    file = request.files.get("file")
    if not file or not file.filename or not file.filename.lower().endswith(".xlsx"):
        raise ValueError("Choose a valid .xlsx class-event export.")
    payload = file.read(20 * 1024 * 1024 + 1)
    if len(payload) > 20 * 1024 * 1024:
        raise ValueError("The Excel file exceeds the 20 MB limit.")
    return attendance_reports.load(io.BytesIO(payload))

def _attendance_error(exc):
    message = str(exc).strip("'\"") or "Could not read the class-event Excel export."
    return jsonify({"error": message}), 400

@app.post("/api/attendance/analyze")
def attendance_analyze():
    try: return jsonify(attendance_reports.summarize(_attendance_dataframe()))
    except Exception as exc: return _attendance_error(exc)

@app.post("/api/attendance/excel")
def attendance_excel():
    try:
        output = attendance_reports.excel_bytes(_attendance_dataframe(), request.form.get("event_name", "Class Event"))
        return send_file(io.BytesIO(output), as_attachment=True, download_name="attendance_report.xlsx", mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    except Exception as exc: return _attendance_error(exc)

@app.post("/api/attendance/image")
def attendance_image():
    try:
        output = attendance_reports.image_bytes(_attendance_dataframe(), request.form.get("event_name", "Class Event"))
        return send_file(io.BytesIO(output), as_attachment=True, download_name="attendance_report.png", mimetype="image/png")
    except Exception as exc: return _attendance_error(exc)

@app.get("/categories")
def categories():
    try: return jsonify([{"uuid": x.get("categories", {}).get("uuid"), "title": x.get("categories", {}).get("title")} for x in _db().tscategories.find()])
    except Exception as exc: return jsonify({"error": str(exc)}), 503
@app.get("/tests/<category_uuid>")
def tests(category_uuid):
    try:
        item = _db().tscategories.find_one({"categories.uuid": category_uuid})
        if not item: return jsonify({"error": "Category not found"}), 404
        return jsonify([{"uuid": x["uuid"], "title": x.get("title", x["uuid"])} for x in item["categories"].get("tests", [])])
    except Exception as exc: return jsonify({"error": str(exc)}), 503
@app.get("/subjects/<test_uuid>")
def subjects(test_uuid):
    try:
        category = _db().tscategories.find_one({"categories.tests.uuid": test_uuid})
        if not category: return jsonify({"error": "Test not found"}), 404
        test = next((x for x in category["categories"].get("tests", []) if x["uuid"] == test_uuid), {})
        return jsonify([{"id": str(x["_id"]), "uuid": x["uuid"], "title": x.get("title", x["uuid"])} for x in test.get("subjects", [])])
    except Exception as exc: return jsonify({"error": str(exc)}), 503

@app.post("/preview")
def preview():
    file, subject = request.files.get("file"), request.form.get("subject")
    if not _valid_docx(file): return jsonify({"error": "Choose a valid .docx file."}), 400
    job_id, name = str(uuid.uuid4()), secure_filename(file.filename); path = UPLOAD_DIR / f"{job_id}_{name}"; file.save(path)
    try: questions, errors, candidates, warnings = parse_docx(path, subject, image_dir=GENERATED_DIR / job_id / "images")
    except Exception as exc: path.unlink(missing_ok=True); return jsonify({"error": f"Could not read DOCX: {exc}"}), 400
    output_path = GENERATED_DIR / f"{Path(name).stem}.json"
    standard_docx_path = GENERATED_DIR / f"{Path(name).stem}_standardized.docx"
    try: create_standard_docx(candidates, standard_docx_path, MASTER_TEMPLATE)
    except Exception as exc: return jsonify({"error": f"Could not create the standardized DOCX: {exc}"}), 500
    _jobs[job_id] = {"questions": questions, "errors": errors, "candidates": candidates, "subject": subject, "filename": name, "output_path": output_path, "standard_docx_path": standard_docx_path}; _write(output_path, questions); _write(ERROR_FILE, errors)
    record = {"id": job_id, "datetime": datetime.now(timezone.utc).isoformat(), "filename": name, "category": "", "test": "", "subject": subject or "", "total": len(questions), "uploaded": len(questions), "failed": len(errors), "status": "WARNING" if errors else "SUCCESS", "activity": "JSON generated", "errors": errors}
    history = _read(HISTORY_FILE, []); history.insert(0, record); _write(HISTORY_FILE, history)
    image_summary = [{"question_number": candidate["number"], "section": image["section"], "option_label": image.get("option_label"), "source": Path(image["path"]).name if image.get("path") else ""} for candidate in candidates for image in candidate.get("image_records", [])]
    return jsonify({"job_id": job_id, "count": len(questions), "found": len(candidates), "image_count": len(image_summary), "image_summary": image_summary, "warnings": warnings, "errors": errors, "questions": questions, "output_filename": f"{Path(name).stem}.json", "standard_docx_filename": f"{Path(name).stem}_standardized.docx"})

@app.get("/download/<job_id>")
def download_json(job_id):
    job = _jobs.get(job_id)
    if not job or not job["output_path"].is_file(): return jsonify({"error": "Generated JSON file not found. Preview the DOCX again."}), 404
    output_name = f"{Path(job['filename']).stem}.json"
    return send_file(job["output_path"], as_attachment=True, download_name=output_name, mimetype="application/json")

@app.get("/download-standard-docx/<job_id>")
def download_standard_docx(job_id):
    job = _jobs.get(job_id)
    if not job or not job["standard_docx_path"].is_file(): return jsonify({"error": "Standardized DOCX not found. Preview the document again."}), 404
    output_name = f"{Path(job['filename']).stem}_standardized.docx"
    return send_file(job["standard_docx_path"], as_attachment=True, download_name=output_name, mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document")

@app.post("/upload")
def upload():
    job_id, category = request.form.get("job_id"), request.form.get("category", "")
    test, subject = request.form.get("test") or os.environ.get("TEST_UUID"), request.form.get("subject")
    job = _jobs.get(job_id) if job_id else None
    if job: questions, errors, subject = job["questions"], job["errors"], subject or job["subject"]
    else:
        file = request.files.get("file")
        if not _valid_docx(file) or not subject: return jsonify({"error": "A DOCX file and subject are required."}), 400
        questions, errors, _, _ = parse_docx(file, subject)
    ts_uuid = category or os.environ.get("TS_UUID")
    if not ts_uuid or not test: return jsonify({"error": "Category and test are required for upload."}), 400
    if not questions: return jsonify({"error": "No valid questions were found.", "errors": errors}), 400
    try: response = send_questions(questions, ts_uuid, test)
    except (requests.RequestException, RuntimeError) as exc: return jsonify({"status": "failed", "count": len(questions), "errors": errors, "error": str(exc)}), 502
    record = {"id": str(uuid.uuid4()), "datetime": datetime.now(timezone.utc).isoformat(), "category": category, "test": test, "subject": subject, "total": len(questions), "uploaded": len(questions), "failed": len(errors), "status": "WARNING" if errors else "SUCCESS", "errors": errors}
    history = _read(HISTORY_FILE, []); history.insert(0, record); _write(HISTORY_FILE, history); _write(ERROR_FILE, errors)
    return jsonify({"status": record["status"].lower(), "count": len(questions), "uploaded": len(questions), "errors": errors, "api_response": response, "record": record})

@app.get("/api/history")
def api_history(): return jsonify(_read(HISTORY_FILE, []))
@app.get("/api/errors")
def api_errors(): return jsonify(_read(ERROR_FILE, []))
@app.errorhandler(413)
def too_large(_): return jsonify({"error": "DOCX files must be 25 MB or smaller."}), 413
if __name__ == "__main__": app.run(debug=True)
