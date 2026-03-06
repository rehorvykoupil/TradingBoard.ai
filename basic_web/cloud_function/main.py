"""
Google Cloud Function (Python): receives form/early-access submissions and sends one email via Gmail SMTP.
No database — email only. Set GMAIL_USER, GMAIL_APP_PASSWORD, and TO_EMAIL in the function config or Secret Manager.
"""
import json
import os
import random
import re
import smtplib
import string
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Any

import functions_framework
from google.cloud import firestore


CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
# Match first valid-looking email in a string (handles env vars with shell junk like "info@x.com & goto...")
EMAIL_EXTRACT_RE = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")


def sanitize_email(value: str) -> str:
    """Return a single valid email from a string, or empty string. Strips shell junk from env vars."""
    if not value:
        return ""
    value = (value or "").strip()
    if EMAIL_RE.match(value):
        return value
    match = EMAIL_EXTRACT_RE.search(value)
    return match.group(0) if match else ""


_firestore_client = None


def get_db():
    global _firestore_client
    if _firestore_client is None:
        _firestore_client = firestore.Client()
    return _firestore_client


def generate_ref_code(length: int = 8) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(length))


def get_config(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def send_email(subject: str, body_text: str) -> None:
    user = sanitize_email(get_config("GMAIL_USER"))
    password = get_config("GMAIL_APP_PASSWORD")
    to_email = sanitize_email(get_config("TO_EMAIL") or user) or user or "info@tradingboard.ai"
    if not user or not password:
        raise RuntimeError("GMAIL_USER and GMAIL_APP_PASSWORD must be set")
    if not to_email:
        to_email = user
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_email
    msg.attach(MIMEText(body_text, "plain"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(user, password)
        server.sendmail(user, [to_email], msg.as_string())


def send_email_to(recipient: str, subject: str, body_text: str) -> None:
    """Send email to a specific recipient (used for validation feedback copies)."""
    recipient = sanitize_email(recipient or "")
    if not recipient:
        return
    user = sanitize_email(get_config("GMAIL_USER"))
    password = get_config("GMAIL_APP_PASSWORD")
    if not user or not password:
        raise RuntimeError("GMAIL_USER and GMAIL_APP_PASSWORD must be set")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = recipient
    msg.attach(MIMEText(body_text, "plain"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(user, password)
        server.sendmail(user, [recipient], msg.as_string())


def json_response(data: dict, status: int = 200, headers=None):
    h = {**CORS_HEADERS, "Content-Type": "application/json"}
    if headers:
        h.update(headers)
    return (json.dumps(data), status, h)


def get_or_create_participant(
    email: str,
    source: str | None = None,
    notes: str | None = None,
    name: str | None = None,
    surname: str | None = None,
    is_mentor: bool | None = None,
) -> tuple[dict, bool]:
    """Ensure a participant doc exists for this email. Returns (data, created). If already exists, do not overwrite."""
    email = (email or "").strip()
    if not email:
        return {}, False
    db = get_db()
    doc_ref = db.collection("participants").document(email)
    snap = doc_ref.get()
    if snap.exists:
        data = snap.to_dict() or {}
        if "email" not in data:
            data["email"] = email
        return data, False

    # New participant only: assign joinedIndex, refCode, and optional source/notes/name/surname
    try:
        existing = list(db.collection("participants").stream())
        joined_index = len(existing) + 1
    except Exception:
        joined_index = 1

    ref_code = generate_ref_code()
    data = {
        "email": email,
        "joinedIndex": joined_index,
        "referrals": 0,
        "refCode": ref_code,
        "createdAt": firestore.SERVER_TIMESTAMP,
    }
    if source:
        data["source"] = source
    if notes is not None:
        data["notes"] = notes
    if name is not None:
        data["name"] = name
    if surname is not None:
        data["surname"] = surname
    if is_mentor is not None:
        data["isMentor"] = bool(is_mentor)
    doc_ref.set(data)
    return data, True


def compute_ranks() -> dict:
    """Compute effective ranks for all participants based on join order and referrals.

    Rank value = joinedIndex - referrals * 50 (lower is better).
    Returns mapping {email: rank_position}.
    """
    db = get_db()
    participants = []
    try:
        for snap in db.collection("participants").stream():
            d = snap.to_dict() or {}
            email = d.get("email") or snap.id
            try:
                joined = int(d.get("joinedIndex") or 0)
            except Exception:
                joined = 0
            try:
                refs = int(d.get("referrals") or 0)
            except Exception:
                refs = 0
            # effective position: lower is better
            rank_value = joined - refs * 50
            participants.append(
                {
                    "email": email,
                    "joinedIndex": joined,
                    "referrals": refs,
                    "rank_value": rank_value,
                }
            )
    except Exception:
        return {}

    participants.sort(key=lambda p: (p["rank_value"], p["joinedIndex"]))
    ranks: dict[str, int] = {}
    for idx, p in enumerate(participants, start=1):
        ranks[p["email"]] = idx
    return ranks


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
    if action not in ("earlyaccess", "contact", "validation", "applicants", "myposition"):
        return json_response({"error": "Invalid or missing action"}, 400)

    if action == "earlyaccess":
        email = (payload.get("email") or "").strip()
        source = (payload.get("source") or "M1").strip().upper()
        if source not in ("M1", "M2"):
            source = "M1"
        if not email or not EMAIL_RE.match(email):
            return json_response({"error": "Valid email is required"}, 400)
        try:
            participant, created = get_or_create_participant(email, source=source)
            if not created:
                # Duplicate: ignore, do not overwrite or send link again
                return json_response({"success": True}, 200)
            # New participant: notify owner and send user their referral link
            send_email(
                subject="[TradingBoard.ai] Early access request",
                body_text=f"Early access signup (source {source}):\n\nEmail: {email}",
            )
            ref_code = participant.get("refCode") or ""
            invite_link = f"https://tradingboard.ai/?ref={ref_code}#validation"
            send_email_to(
                recipient=email,
                subject="[TradingBoard.ai] Thanks for your interest",
                body_text=f"Thanks! We'll be in touch.\n\nShare your link to move up the leaderboard:\n{invite_link}\n(1 referral = +50 positions)\n",
            )
            return json_response({"success": True}, 200)
        except Exception as e:
            return json_response({"error": "Server error", "detail": str(e)}, 500)

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

    if action == "validation":
        email = (payload.get("email") or "").strip()
        notes = (payload.get("notes") or "").strip()
        ref_code_in = (payload.get("ref") or "").strip().lower()
        name = (payload.get("name") or "").strip()
        surname = (payload.get("surname") or "").strip()
        is_mentor = payload.get("isMentor") is True
        if not email or not EMAIL_RE.match(email):
            return json_response({"error": "Valid email is required"}, 400)

        # Create participant only if new (source V1). Duplicate = ignore, no overwrite.
        participant, created = get_or_create_participant(
            email, source="V1", notes=notes or None, name=name or None, surname=surname or None, is_mentor=is_mentor
        )
        db = get_db()

        # Handle referral: only if a valid ref code is present AND this is a new participant (first-time submit).
        # If the email was already submitted (duplicate), do not credit the referrer — ignore.
        referrer_email = None
        if ref_code_in and created:
            try:
                query = (
                    db.collection("participants")
                    .where("refCode", "==", ref_code_in)
                    .limit(1)
                )
                for snap in query.stream():
                    referrer_email = snap.id
                    if referrer_email and referrer_email.strip().lower() == email.strip().lower():
                        referrer_email = None  # don't count self-referral
                        break
                    d = snap.to_dict() or {}
                    current_refs = int(d.get("referrals") or 0)
                    snap.reference.update({"referrals": current_refs + 1})
                    break
            except Exception:
                referrer_email = None

        # Compute ranks after potential referral update
        ranks = compute_ranks()
        total_participants = len(ranks) or 1
        user_rank = ranks.get(email)
        ref_rank = ranks.get(referrer_email) if referrer_email else None

        # Build invite link using user's personal ref code
        user_ref_code = participant.get("refCode") or generate_ref_code()
        invite_link = f"https://tradingboard.ai/?ref={user_ref_code}#validation"
        myposition_link = f"https://tradingboard.ai/?ref={user_ref_code}#myposition"

        # Simple tier text based on rank (static thresholds)
        def tier_for(rank: int | None) -> str:
            if not rank:
                return "Early supporter tier"
            if rank <= 1:
                return "Platinum (Top 1)"
            if rank <= 5:
                return "Gold (Top 5)"
            if rank <= 10:
                return "Top 10"
            if rank <= 25:
                return "Top 25"
            if rank <= 50:
                return "Top 50"
            if rank <= 100:
                return "Top 100"
            return "Early supporter tier"

        user_tier = tier_for(user_rank)

        success_text = "Thank you for your submission!\n\n"
        success_text += f"Email: {email}\n\n"
        success_text += "User notes:\n"
        success_text += (notes or "(none)") + "\n\n"
        if user_rank:
            success_text += f"Your Current Status: Rank #{user_rank} out of ~{total_participants} participants. Current Standing: {user_tier}.\n\n"
        success_text += (
            "Don't get bumped! Rankings are live. If others refer more members, your rank will drop. "
            "To climb the leaderboard and protect your tier, invite another expert to help us validate the product.\n\n"
            f"Your Unique Invite Link: {invite_link}\n"
            "(1 referral = +50 positions)\n\n"
            f"You can always find your actual position with this link: {myposition_link}\n\n"
            "Please note: the final ranking will be frozen at the moment of the official beta release.\n\n"
            "Kind regards,\n"
            "Your TradingBoard.ai team\n"
        )

        body_lines = [
            "Submission:",
            f"Email: {email}",
        ]
        if notes:
            body_lines.append("")
            body_lines.append("User notes:")
            body_lines.append(notes)
        body_lines.append("")
        body_lines.append(success_text)
        body = "\n".join(body_lines)

        try:
            # Always notify site owner of validation submission
            send_email(
                subject="[TradingBoard.ai] Validation feedback",
                body_text=body,
            )
            # Send link to user only when newly created (first time)
            if created:
                send_email_to(
                    recipient=email,
                    subject="[TradingBoard.ai] Thanks for your submission",
                    body_text=success_text,
                )

            # Notify referrer if any
            if referrer_email and ref_rank:
                ref_success = (
                    f"Your referral just submitted validation feedback.\n\n"
                    f"Your updated effective rank is approximately #{ref_rank} out of ~{total_participants} participants.\n\n"
                    f"Keep sharing your link to climb the leaderboard:\n{invite_link}\n"
                    "(1 referral = +50 positions)\n"
                )
                try:
                    send_email_to(
                        recipient=referrer_email,
                        subject="[TradingBoard.ai] Your rank just moved up",
                        body_text=ref_success,
                    )
                except Exception:
                    pass
            return json_response({"success": True}, 200)
        except Exception as e:
            return json_response({"error": "Server error", "detail": str(e)}, 500)

    if action == "myposition":
        ref_code_in = (payload.get("ref") or "").strip().lower()
        if not ref_code_in:
            return json_response({"error": "Missing ref (referral code)"}, 400)
        db = get_db()
        try:
            query = (
                db.collection("participants")
                .where("refCode", "==", ref_code_in)
                .limit(1)
            )
            snap = None
            for s in query.stream():
                snap = s
                break
            if not snap:
                return json_response({"error": "Invalid or unknown referral code"}, 404)
            d = snap.to_dict() or {}
            email = d.get("email") or snap.id
            ranks = compute_ranks()
            rank = ranks.get(email)
            total = len(ranks) or 0
            def tier_for(r):
                if not r: return "—"
                if r <= 1: return "Platinum (Top 1)"
                if r <= 5: return "Gold (Top 5)"
                if r <= 10: return "Top 10"
                if r <= 25: return "Top 25"
                if r <= 50: return "Top 50"
                if r <= 100: return "Top 100"
                return "Early supporter tier"
            return json_response({
                "email": email,
                "name": d.get("name") or "",
                "surname": d.get("surname") or "",
                "source": d.get("source") or "",
                "notes": d.get("notes") or "",
                "isMentor": bool(d.get("isMentor")),
                "rank": rank,
                "totalParticipants": total,
                "tier": tier_for(rank),
            }, 200)
        except Exception as e:
            return json_response({"error": "Server error", "detail": str(e)}, 500)

    if action == "applicants":
        db = get_db()
        applicants = []
        try:
            ranks = compute_ranks()
            participants = list(db.collection("participants").stream())
            # Sort by rank (1 first)
            def _rank_order(snap):
                d = snap.to_dict() or {}
                email = d.get("email") or snap.id
                r = ranks.get(email)
                if r is None:
                    return (999999, 0)
                return (r, int(d.get("joinedIndex") or 0))
            participants.sort(key=_rank_order)

            for idx, snap in enumerate(participants, start=1):
                d = snap.to_dict() or {}
                email = d.get("email") or snap.id
                source = d.get("source") or ""
                notes = d.get("notes") or ""
                name = d.get("name") or ""
                surname = d.get("surname") or ""
                # Fallback: parse name from notes (legacy V1 format "Name: First Last")
                if not name and notes:
                    for line in notes.splitlines():
                        if line.lower().startswith("name:"):
                            full = line.split(":", 1)[1].strip()
                            parts = full.split()
                            if parts:
                                name = parts[0]
                                if len(parts) > 1:
                                    surname = " ".join(parts[1:])
                            break
                applicants.append({
                    "order": idx,
                    "email": email,
                    "source": source,
                    "name": name,
                    "surname": surname,
                    "notes": notes,
                })
        except Exception:
            applicants = []

        return json_response({"applicants": applicants}, 200)

    return json_response({"error": "Bad request"}, 400)
