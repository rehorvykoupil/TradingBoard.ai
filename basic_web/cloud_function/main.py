"""
Google Cloud Function (Python): receives form/early-access submissions and sends one email via Gmail SMTP.
No database — email only. Set GMAIL_USER, GMAIL_APP_PASSWORD, and TO_EMAIL in the function config or Secret Manager.
"""
import json
import os
import re
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import functions_framework


CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def get_config(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def send_email(subject: str, body_text: str) -> None:
    user = get_config("GMAIL_USER")
    password = get_config("GMAIL_APP_PASSWORD")
    to_email = get_config("TO_EMAIL") or user or "info@tradingboard.ai"
    if not user or not password:
        raise RuntimeError("GMAIL_USER and GMAIL_APP_PASSWORD must be set")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_email
    msg.attach(MIMEText(body_text, "plain"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(user, password)
        server.sendmail(user, [to_email], msg.as_string())


def json_response(data: dict, status: int = 200, headers=None):
    h = {**CORS_HEADERS, "Content-Type": "application/json"}
    if headers:
        h.update(headers)
    return (json.dumps(data), status, h)


@functions_framework.http
def send_form_email(request):
    # CORS preflight
    if request.method == "OPTIONS":
        return ("", 204, CORS_HEADERS)

    if request.method != "POST":
        return json_response({"error": "Method not allowed"}, 405)

    try:
        payload = request.get_json(silent=True) or {}
    except Exception:
        payload = {}

    action = (payload.get("action") or "").strip().lower()
    if action not in ("earlyaccess", "contact"):
        return json_response({"error": "Invalid or missing action (use earlyAccess or contact)"}, 400)

    if action == "earlyaccess":
        email = (payload.get("email") or "").strip()
        if not email or not EMAIL_RE.match(email):
            return json_response({"error": "Valid email is required"}, 400)
        try:
            send_email(
                subject="[TradingBoard.ai] Early access request",
                body_text=f"Early access signup:\n\nEmail: {email}",
            )
            return json_response({"success": True}, 200)
        except Exception as e:
            return json_response({"error": "Server error"}, 500)

    if action == "contact":
        name = (payload.get("name") or "").strip()
        email = (payload.get("email") or "").strip()
        contact_type = (payload.get("type") or "").strip() or "Individual Trader"
        message = (payload.get("message") or "").strip()
        if not name or not email:
            return json_response({"error": "Name and email are required"}, 400)
        if not EMAIL_RE.match(email):
            return json_response({"error": "Valid email is required"}, 400)
        try:
            body = f"Name: {name}\nEmail: {email}\nType: {contact_type}\n\nMessage:\n{message or '(none)'}"
            send_email(
                subject="[TradingBoard.ai] Contact form submission",
                body_text=body,
            )
            return json_response({"success": True}, 200)
        except Exception as e:
            return json_response({"error": "Server error"}, 500)

    return json_response({"error": "Bad request"}, 400)
