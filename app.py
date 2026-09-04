import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, send_file
from flask_cors import CORS
from pymongo import MongoClient
from werkzeug.utils import secure_filename

from parser import create_standard_docx, parse_docx

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
app = Flask(__name__); app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024; app.secret_key = os.environ.get("FLASK_SECRET_KEY", "local-development-only")
CORS(app); _jobs = {}; _client = None

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
    _jobs[job_id] = {"questions": questions, "errors": errors, "subject": subject, "filename": name, "output_path": output_path, "standard_docx_path": standard_docx_path}; _write(output_path, questions); _write(ERROR_FILE, errors)
    record = {"id": job_id, "datetime": datetime.now(timezone.utc).isoformat(), "filename": name, "category": "", "test": "", "subject": subject or "", "total": len(questions), "uploaded": len(questions), "failed": len(errors), "status": "WARNING" if errors else "SUCCESS", "activity": "JSON generated", "errors": errors}
    history = _read(HISTORY_FILE, []); history.insert(0, record); _write(HISTORY_FILE, history)
    return jsonify({"job_id": job_id, "count": len(questions), "found": len(candidates), "warnings": warnings, "errors": errors, "questions": questions, "output_filename": f"{Path(name).stem}.json", "standard_docx_filename": f"{Path(name).stem}_standardized.docx"})

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
