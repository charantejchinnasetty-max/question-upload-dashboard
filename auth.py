"""Login for the dashboard: user ID = mobile number, shared password from the APP_PASSWORD env var.

Settings (Vercel → Project → Settings → Environment Variables):
  APP_PASSWORD     required. The login password. Never commit it — this repository is public.
  ALLOWED_MOBILES  optional. Comma-separated staff mobile numbers allowed to sign in.
                   If empty, any valid 10-digit Indian mobile number can sign in with the password.
  FLASK_SECRET_KEY optional. Signs the session cookie. If missing, one is derived from other secrets.
If APP_PASSWORD is not set, nobody can sign in (the site stays locked).
"""
import hashlib
import hmac
import os
import re
import time
from datetime import timedelta
from urllib.parse import urlparse

from flask import jsonify, redirect, render_template, request, session, url_for

PUBLIC_PATHS = {"/login", "/favicon.ico"}
_attempts = {}  # ip -> [timestamps]; per-instance throttle against password guessing
MAX_ATTEMPTS, WINDOW = 8, 300


def normalize_mobile(value):
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return digits if re.fullmatch(r"[6-9]\d{9}", digits) else None


def allowed_mobiles():
    raw = os.environ.get("ALLOWED_MOBILES", "")
    return {m for m in (normalize_mobile(x) for x in raw.split(",")) if m}


def _secret_key():
    explicit = os.environ.get("FLASK_SECRET_KEY")
    if explicit:
        return explicit
    material = "|".join(os.environ.get(k, "") for k in ("APP_PASSWORD", "DATABASE_URL", "MONGO_URI", "API_PASSWORD"))
    if not material.strip("|"):
        return "local-development-only"
    return hashlib.sha256(("asc-session-v1|" + material).encode()).hexdigest()


def _throttled(ip):
    now = time.time()
    recent = [t for t in _attempts.get(ip, []) if now - t < WINDOW]
    _attempts[ip] = recent
    return len(recent) >= MAX_ATTEMPTS


def _client_ip():
    return (request.headers.get("x-forwarded-for") or request.remote_addr or "?").split(",")[0].strip()


def _safe_next(target):
    if not target or not target.startswith("/") or target.startswith("//"):
        return "/"
    return target


def init_auth(app):
    app.secret_key = _secret_key()
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=bool(os.environ.get("VERCEL")),
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    )

    @app.before_request
    def require_login():
        path = request.path
        if path in PUBLIC_PATHS or path.startswith("/static/"):
            return None
        if not session.get("mobile"):
            if path.startswith("/api/"):
                return jsonify({"error": "Your session has ended. Please sign in again."}), 401
            return redirect(url_for("login_page", next=request.full_path.rstrip("?")))
        # Block cross-site form/API posts (CSRF): the Origin must be this site.
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("Origin")
            if origin and urlparse(origin).netloc != request.host:
                return jsonify({"error": "Cross-site request blocked"}), 403
        return None

    @app.context_processor
    def inject_user():
        return {"current_mobile": session.get("mobile")}

    @app.route("/login", methods=["GET", "POST"])
    def login_page():
        configured = bool(os.environ.get("APP_PASSWORD"))
        next_url = _safe_next(request.values.get("next"))
        if request.method == "GET":
            if session.get("mobile"):
                return redirect(next_url)
            return render_template("login.html", error=None, configured=configured, next=next_url, mobile="")
        ip = _client_ip()
        mobile_raw = request.form.get("mobile", "")
        if not configured:
            return render_template("login.html", error="Login is not set up yet. Add APP_PASSWORD in Vercel settings.", configured=False, next=next_url, mobile=mobile_raw), 503
        if _throttled(ip):
            return render_template("login.html", error="Too many attempts. Please wait 5 minutes and try again.", configured=True, next=next_url, mobile=mobile_raw), 429
        mobile = normalize_mobile(mobile_raw)
        password_ok = hmac.compare_digest(request.form.get("password", "").encode(), os.environ["APP_PASSWORD"].encode())
        allow = allowed_mobiles()
        if not mobile or not password_ok or (allow and mobile not in allow):
            _attempts.setdefault(ip, []).append(time.time())
            msg = "Enter a valid 10-digit mobile number." if not mobile else "Mobile number or password is incorrect."
            app.logger.warning("Failed login for %s from %s", (mobile or "invalid")[-4:], ip)
            return render_template("login.html", error=msg, configured=True, next=next_url, mobile=mobile_raw), 401
        session.clear()
        session.permanent = True
        session["mobile"] = mobile
        session["login_at"] = int(time.time())
        app.logger.info("Login ****%s from %s", mobile[-4:], ip)
        return redirect(next_url)

    @app.route("/logout", methods=["GET", "POST"])
    def logout():
        session.clear()
        return redirect(url_for("login_page"))
