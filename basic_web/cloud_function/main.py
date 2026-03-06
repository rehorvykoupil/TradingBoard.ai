"""
Google Cloud Function (Python): receives form/early-access submissions and sends one email via Gmail SMTP.
No database — email only. Set GMAIL_USER, GMAIL_APP_PASSWORD, and TO_EMAIL in the function config or Secret Manager.
"""

# --- Bumped notification config (hardcoded) ---
# True = send "you've been bumped" email immediately when rank drops.
# False = enqueue and send once per day at BUMPED_EMAIL_TIME (use Cloud Scheduler to call action=send_bumped_digest).
SEND_BUMPED_IMMEDIATELY = True
# When using daily digest: send time in given timezone (e.g. "13:00" = 1:00 PM).
BUMPED_EMAIL_TIME = "13:00"
BUMPED_EMAIL_TIMEZONE = "Europe/Prague"

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
    """Compute effective ranks: base rank by join order, then 1 referral = move up 50 positions (floor 1).

    Base rank = position when sorted by joinedIndex (1, 2, 3, ...).
    Effective rank = max(1, base_rank - referrals*50). Final leaderboard order by effective_rank.
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
            participants.append(
                {
                    "email": email,
                    "joinedIndex": joined,
                    "referrals": refs,
                }
            )
    except Exception:
        return {}

    # Base order: by joinedIndex only → base_rank 1, 2, 3, ...
    participants.sort(key=lambda p: (p["joinedIndex"], p["email"]))
    for idx, p in enumerate(participants, start=1):
        p["base_rank"] = idx
    # Effective rank = max(1, base_rank - referrals*50) — "move up 50 positions per referral"
    for p in participants:
        p["effective_rank"] = max(1, p["base_rank"] - p["referrals"] * 50)
    # Final order: by effective_rank, then by more referrals first (so 1 ref beats 0 when both at rank 1), then joinedIndex
    participants.sort(key=lambda p: (p["effective_rank"], -p["referrals"], p["joinedIndex"]))
    ranks: dict[str, int] = {}
    for idx, p in enumerate(participants, start=1):
        ranks[p["email"]] = idx
    return ranks


def tier_display_name(rank: int | None) -> str:
    """Tier label for emails (e.g. 'Top 50 Tier')."""
    if not rank:
        return "Early supporter tier"
    if rank <= 1:
        return "Platinum Tier (Top 1)"
    if rank <= 5:
        return "Gold Tier (Top 5)"
    if rank <= 10:
        return "Top 10 Tier"
    if rank <= 25:
        return "Top 25 Tier"
    if rank <= 50:
        return "Top 50 Tier"
    if rank <= 100:
        return "Top 100 Tier"
    return "Early supporter tier"


def next_tier_name(rank: int | None) -> str:
    """Next tier up (e.g. 'Top 25' for someone in Top 50)."""
    if not rank or rank <= 1:
        return "Platinum Tier (Top 1)"
    if rank <= 5:
        return "Platinum Tier (Top 1)"
    if rank <= 10:
        return "Gold Tier (Top 5)"
    if rank <= 25:
        return "Top 10 Tier"
    if rank <= 50:
        return "Top 25 Tier"
    if rank <= 100:
        return "Top 50 Tier"
    return "Top 100 Tier"


def desired_reward(rank: int | None) -> str:
    """Reward text for the tier above (e.g. '75% Discount')."""
    if not rank or rank <= 1:
        return "100% Discount"
    if rank <= 5:
        return "100% Discount"
    if rank <= 10:
        return "75% Discount"
    if rank <= 25:
        return "50% Discount"
    if rank <= 50:
        return "25% Discount"
    if rank <= 100:
        return "15% Discount"
    return "Founding Member benefits"


def send_bumped_email(
    recipient_email: str,
    first_name: str,
    old_rank: int,
    new_rank: int,
    ref_code: str,
) -> None:
    """Send the 'you have been bumped' notification email."""
    invite_link = f"https://tradingboard.ai/?ref={ref_code}#validation"
    myposition_link = f"https://tradingboard.ai/?ref={ref_code}#myposition"
    unsubscribe_link = f"https://tradingboard.ai/?ref={ref_code}#unsubscribe"

    tier_name = tier_display_name(new_rank)
    next_tier = next_tier_name(new_rank)
    reward = desired_reward(new_rank)

    subject = f"Action Required: You've been bumped! ⚠️ | New Rank: #{new_rank}"

    body = f"""Hello {first_name},

Quick update on the TradingBoard.ai leaderboard: Another expert has just joined the "Old Guard" and moved up the ranks.

As a result, your standing has changed:

Previous Rank: #{old_rank}

NEW CURRENT RANK: #{new_rank}

Current Standing: {tier_name}

Don't lose your Founding Member benefits!
You have dropped in the rankings, moving you further away from the {next_tier} rewards. If you want to reclaim your position and protect your {reward}, now is the time to act.

Jump back up by 50 positions:
Simply share your unique invite link with one colleague. Once they validate the product, you'll leapfrog back toward the top of the leaderboard.

Your Unique Invite Link:
👉 {invite_link}

Track the live leaderboard here: {myposition_link}

Note: The final ranking will be frozen at the moment of the official beta release. Protect your rank before the clock runs out.

Best regards,

Rehor Vykoupil CEO • tradingboard.ai

Manage your notifications: You are receiving this because you participated in the TradingBoard.ai strategic validation. If you wish to stop receiving live rank updates and leaderboard alerts, you can [Unsubscribe from these alerts here]: {unsubscribe_link}

Please note: Unsubscribing from alerts will not remove your position from the leaderboard; you simply won't be notified if your rank changes."""

    send_email_to(recipient=recipient_email, subject=subject, body_text=body)


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
    if action not in ("earlyaccess", "contact", "validation", "applicants", "myposition", "unsubscribe", "send_bumped_digest"):
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
                body_text=(
                    "Thanks! We'll be in touch.\n\n"
                    "Jump back up:\n"
                    "Simply share your unique invite link with a colleague. Once they validate the product, you'll leapfrog back toward the top. "
                    "More referrals = better position. Rank is determined primarily by your number of successful referrals; "
                    "the date you joined only serves as a tie-breaker for members with the same referral count.\n\n"
                    f"Your unique invite link:\n{invite_link}\n"
                ),
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

        # Record when they pressed Submit feedback (every time)
        try:
            db.collection("participants").document(email).set(
                {"validationSubmittedAt": firestore.SERVER_TIMESTAMP},
                merge=True,
            )
        except Exception:
            pass

        # Ranks *before* any referral update (to detect who gets bumped later)
        ranks_before = compute_ranks()

        # Handle referral: only if a valid ref code is present AND this is a new participant (first-time submit).
        # If the email was already submitted (duplicate), do not credit the referrer — ignore.
        # We also record who invited this participant (referredByEmail, referredByRef).
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
                    # Store who invited this participant
                    db.collection("participants").document(email).set(
                        {
                            "referredByEmail": referrer_email,
                            "referredByRef": ref_code_in,
                        },
                        merge=True,
                    )
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

        # Reply email to user: subject and body (dynamic rank, tier, links)
        rank_label = f"#{user_rank}" if user_rank else "#—"
        reply_subject = f"You're in! Your TradingBoard.ai Rank: {rank_label}"
        tier_display = user_tier
        if user_rank:
            if user_rank <= 1:
                tier_display = "Platinum Tier (Top 1)"
            elif user_rank <= 5:
                tier_display = "Gold Tier (Top 5)"

        reply_body = f"""You're in! Your TradingBoard.ai Rank: {rank_label}

Thank you for your submission! We are already reviewing your insights.

Your Current Status: Rank {rank_label}
Current Standing: {tier_display}

Don't get bumped! Rankings are live. If others refer more members, your rank will drop. To climb the leaderboard and protect your tier, invite another expert to help us validate the product.

Jump back up:
Simply share your unique invite link with a colleague. Once they validate the product, you'll leapfrog back toward the top. More referrals = better position. Rank is determined primarily by your number of successful referrals; the date you joined only serves as a tie-breaker for members with the same referral count.

Your Unique Invite Link: 👉 {invite_link}

Track your live position here: {myposition_link}

Please note: The final ranking will be frozen at the moment of the official beta release.

Best regards,

Rehor Vykoupil
CEO • tradingboard.ai
"""

        body_lines = [
            "Submission:",
            f"Email: {email}",
        ]
        if notes:
            body_lines.append("")
            body_lines.append("User notes:")
            body_lines.append(notes)
        body_lines.append("")
        body_lines.append(reply_body)
        body = "\n".join(body_lines)

        try:
            # Always notify site owner of validation submission
            send_email(
                subject="[TradingBoard.ai] Validation feedback",
                body_text=body,
            )
            # Send reply to user only when newly created (first time)
            if created:
                send_email_to(
                    recipient=email,
                    subject=reply_subject,
                    body_text=reply_body,
                )

            # Notify referrer if any
            if referrer_email and ref_rank:
                ref_success = (
                    f"Your referral just submitted validation feedback.\n\n"
                    f"Your updated effective rank is approximately #{ref_rank} out of ~{total_participants} participants.\n\n"
                    "Keep sharing your link to climb the leaderboard.\n"
                    "More referrals = better position. Rank is determined primarily by your number of successful referrals; "
                    "the date you joined only serves as a tie-breaker for members with the same referral count.\n\n"
                    f"Your unique invite link:\n{invite_link}\n"
                )
                try:
                    send_email_to(
                        recipient=referrer_email,
                        subject="[TradingBoard.ai] Your rank just moved up",
                        body_text=ref_success,
                    )
                except Exception:
                    pass

            # Bumped notifications: anyone whose rank got worse (higher number) after this validation
            bumped = {}
            for em, old_r in ranks_before.items():
                new_r = ranks.get(em)
                if new_r is not None and new_r > old_r:
                    bumped[em] = (old_r, new_r)

            for bumped_email, (old_rank, new_rank) in bumped.items():
                try:
                    snap = db.collection("participants").document(bumped_email).get()
                    if not snap.exists:
                        continue
                    d = snap.to_dict() or {}
                    if d.get("unsubscribedFromRankAlerts"):
                        continue
                    ref_code_b = (d.get("refCode") or "").strip()
                    if not ref_code_b:
                        continue
                    first_name = (d.get("name") or "").strip() or "there"
                    if SEND_BUMPED_IMMEDIATELY:
                        send_bumped_email(
                            recipient_email=bumped_email,
                            first_name=first_name,
                            old_rank=old_rank,
                            new_rank=new_rank,
                            ref_code=ref_code_b,
                        )
                    else:
                        db.collection("bumped_pending").document(bumped_email).set({
                            "oldRank": old_rank,
                            "newRank": new_rank,
                            "detectedAt": firestore.SERVER_TIMESTAMP,
                        })
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
                refs = int(d.get("referrals") or 0)
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
                t = d.get("validationSubmittedAt")
                submitted_at = t.isoformat() if (t and hasattr(t, "isoformat")) else None
                referred_by_email = d.get("referredByEmail") or ""
                referred_by_ref = d.get("referredByRef") or ""
                applicants.append({
                    "order": idx,
                    "email": email,
                    "source": source,
                    "name": name,
                    "surname": surname,
                    "notes": notes,
                    "submittedAt": submitted_at,
                    "referrals": refs,
                    "referredByEmail": referred_by_email,
                    "referredByRef": referred_by_ref,
                })
        except Exception:
            applicants = []

        return json_response({"applicants": applicants}, 200)

    if action == "unsubscribe":
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
            for snap in query.stream():
                snap.reference.update({"unsubscribedFromRankAlerts": True})
                return json_response({"success": True, "message": "Unsubscribed from rank alerts."}, 200)
            return json_response({"error": "Invalid or unknown referral code"}, 404)
        except Exception as e:
            return json_response({"error": "Server error", "detail": str(e)}, 500)

    if action == "send_bumped_digest":
        # Called by Cloud Scheduler at BUMPED_EMAIL_TIME (e.g. 13:00 CET). Sends one email per pending bumped user.
        db = get_db()
        try:
            pending = list(db.collection("bumped_pending").stream())
            for snap in pending:
                bumped_email = snap.id
                d = snap.to_dict() or {}
                old_rank = int(d.get("oldRank") or 0)
                new_rank = int(d.get("newRank") or 0)
                if not bumped_email or not old_rank or not new_rank:
                    snap.reference.delete()
                    continue
                part_snap = db.collection("participants").document(bumped_email).get()
                if not part_snap.exists:
                    snap.reference.delete()
                    continue
                part = part_snap.to_dict() or {}
                if part.get("unsubscribedFromRankAlerts"):
                    snap.reference.delete()
                    continue
                ref_code_b = (part.get("refCode") or "").strip()
                if not ref_code_b:
                    snap.reference.delete()
                    continue
                # Use current rank in case it changed again
                ranks = compute_ranks()
                current_rank = ranks.get(bumped_email) or new_rank
                first_name = (part.get("name") or "").strip() or "there"
                try:
                    send_bumped_email(
                        recipient_email=bumped_email,
                        first_name=first_name,
                        old_rank=old_rank,
                        new_rank=current_rank,
                        ref_code=ref_code_b,
                    )
                except Exception:
                    pass
                snap.reference.delete()
            return json_response({"success": True}, 200)
        except Exception as e:
            return json_response({"error": "Server error", "detail": str(e)}, 500)

    return json_response({"error": "Bad request"}, 400)
