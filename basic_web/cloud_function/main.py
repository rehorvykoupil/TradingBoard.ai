"""
Google Cloud Function (Python): receives form/early-access submissions and sends one email via Gmail SMTP.
No database — email only. Set GMAIL_USER, GMAIL_APP_PASSWORD, and TO_EMAIL in the function config or Secret Manager.
"""

# --- Standing notification config ---
# Rank-change emails (moved up / bumped down) are sent via a daily digest.
# Cloud Scheduler should call action=send_bumped_digest periodically (e.g. every 5–15 minutes).
# The function will only send emails when the current UTC time matches STANDING_EMAIL_TIME.
DEFAULT_STANDING_EMAIL_TIME = "14:00"  # 14:00 GMT/UTC
STANDING_EMAIL_TIMEZONE = "UTC"

import json
import os
import random
import re
import smtplib
import string
import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Any

import functions_framework
from google.cloud import firestore
try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - fallback for old runtimes
    ZoneInfo = None


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
    # Always notify the owner mailbox at info@tradingboard.ai
    to_email = sanitize_email(get_config("TO_EMAIL") or "info@tradingboard.ai") or "info@tradingboard.ai"
    if not user or not password:
        raise RuntimeError("GMAIL_USER and GMAIL_APP_PASSWORD must be set")
    if not to_email:
        to_email = user
    # Visible sender: FROM_EMAIL or support@tradingboard.ai. GMAIL_USER is only for SMTP login.
    # support@ must be added as "Send mail as" on the GMAIL_USER account or Gmail may reject (550).
    from_email = sanitize_email(get_config("FROM_EMAIL") or "support@tradingboard.ai") or user
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_email
    msg["To"] = to_email
    msg.attach(MIMEText(body_text, "plain"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(user, password)
        server.sendmail(from_email, [to_email], msg.as_string())


def send_email_to(recipient: str, subject: str, body_text: str) -> None:
    """Send email to a specific recipient (used for validation feedback copies)."""
    recipient = sanitize_email(recipient or "")
    if not recipient:
        return
    user = sanitize_email(get_config("GMAIL_USER"))
    password = get_config("GMAIL_APP_PASSWORD")
    if not user or not password:
        raise RuntimeError("GMAIL_USER and GMAIL_APP_PASSWORD must be set")
    # Visible sender: FROM_EMAIL or support@tradingboard.ai. GMAIL_USER is only for SMTP login.
    # support@ must be added as "Send mail as" on the GMAIL_USER account or Gmail may reject (550).
    from_email = sanitize_email(get_config("FROM_EMAIL") or "support@tradingboard.ai") or user
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_email
    msg["To"] = recipient
    msg.attach(MIMEText(body_text, "plain"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(user, password)
        server.sendmail(from_email, [recipient], msg.as_string())


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


def mask_email_for_public(email: str) -> str:
    """Return a masked version of the email for public views (keeps domain, hides most of local part)."""
    email = (email or "").strip()
    if not email or "@" not in email:
        return ""
    local, domain = email.split("@", 1)
    if not local:
        return "@" + domain
    if len(local) <= 2:
        masked_local = local[0] + "***"
    else:
        masked_local = local[0] + "***" + local[-1]
    return masked_local + "@" + domain


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
            # Gamification only for participants who opted into early access.
            # Treat missing flag as True (backwards compatible); explicit False = no gamification.
            early_opt = d.get("earlyAccessOptIn")
            if early_opt is False:
                continue
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
    """Reward text for the participant's current tier, matching the public reward table."""
    if not rank:
        return "Access to the Private Betas. Be the first to trade on the engine."
    if rank <= 1:
        return "Founding Member Status + 100% Discount (10 YEARS) + 50,000 Compute Credits."
    if rank <= 5:
        return "Founding Member Status + 100% Discount (3 years) + 10,000 Compute Credits."
    if rank <= 10:
        return "Founding Member Status + 75% Discount (3 years) + 10,000 Compute Credits."
    if rank <= 25:
        return "Founding Member Status + 75% Discount (3 years) + 1,000 Compute Credits."
    if rank <= 50:
        return "Founding Member Status + 75% Discount (3 years)."
    if rank <= 100:
        return "Founding Member Status + 50% Discount (3 years)."
    return "Access to the Private Betas. Be the first to trade on the engine."


def send_bumped_email(
    recipient_email: str,
    first_name: str,
    old_rank: int,
    new_rank: int,
    ref_code: str,
) -> None:
    """Send the 'you have been bumped' notification email."""
    invite_link = f"https://tradingboard.ai/?ref={ref_code}#early_access"
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

Jump back up:
Simply share your unique invite link with one colleague. Once they validate the product, you'll leapfrog back toward the top of the leaderboard. More referrals = better position. Rank is determined primarily by your number of successful referrals; the date you joined only serves as a tie-breaker for members with the same referral count.

Your Unique Invite Link:
👉 {invite_link}

Track the live leaderboard here: {myposition_link}

Note: The final ranking will be frozen at the moment of the official beta release. Protect your rank before the clock runs out.

Your current reward based on this rank: {reward}.

Best regards,

Rehor Vykoupil CEO • tradingboard.ai

Manage your notifications: You are receiving this because you participated in the TradingBoard.ai strategic validation. If you wish to stop receiving live rank updates and leaderboard alerts, you can [Unsubscribe from these alerts here]: {unsubscribe_link}

Please note: Unsubscribing from alerts will not remove your position from the leaderboard; you simply won't be notified if your rank changes."""

    send_email_to(recipient=recipient_email, subject=subject, body_text=body)


def send_rank_up_email(
    recipient_email: str,
    new_rank: int,
    ref_code: str,
) -> None:
    """Send the 'your rank moved up' notification email."""
    tier = tier_display_name(new_rank)
    reward = desired_reward(new_rank)
    rank_label = f"#{new_rank}"
    reward_tiers_block = (
        "The Reward Tiers:\n"
        "    - ALL: Access to the Private Betas. Be the first to trade on the engine.\n"
        "    - TOP 100: Founding Member Status + 50% Discount (3 years).\n"
        "    - TOP 50: Founding Member Status + 75% Discount (3 years).\n"
        "    - TOP 25: Founding Member Status + 75% Discount (3 years) + 1,000 Compute Credits.\n"
        "    - TOP 10: Founding Member Status + 75% Discount (3 years) + 10,000 Compute Credits.\n"
        "    - TOP 5 (Gold): Founding Member Status + 100% Discount (3 years) + 10,000 Compute Credits.\n"
        "    - TOP 1 (Platinum): Founding Member Status + 100% Discount (10 YEARS) + 50,000 Compute Credits.\n"
    )
    invite_link = f"https://tradingboard.ai/?ref={ref_code}#early_access"
    myposition_link = f"https://tradingboard.ai/?ref={ref_code}#myposition"

    body = (
        "Your referral just joined Early Access and confirmed their email.\n\n"
        f"Your TradingBoard.ai Rank: {rank_label}\n\n"
        "Thank you for requesting early access to TradingBoard.ai.\n\n"
        f"Your Current Status: Rank {rank_label}\n"
        f"Current Standing: {tier}\n\n"
        "Don't get bumped! Rankings are live. If others refer more members, your rank will drop. "
        "To climb the leaderboard and protect your tier, invite another expert to help us validate the product.\n\n"
        "Jump back up:\n"
        "Simply share your unique invite link with a colleague. Once they validate the product, you'll leapfrog back toward the top. "
        "More referrals = better position. Rank is determined primarily by your number of successful referrals; "
        "the date you joined only serves as a tie-breaker for members with the same referral count.\n\n"
        f"Your Unique Invite Link: 👉 {invite_link}\n\n"
        f"Track your live position here: {myposition_link}\n\n"
        "Please note: The final ranking will be frozen at the moment of the official beta release.\n\n"
        f"As a member of the {tier}, your current reward is: {reward}\n\n"
        f"{reward_tiers_block}\n"
        "Best regards,\n\n"
        "Rehor Vykoupil\n"
        "CEO • tradingboard.ai\n"
    )

    send_email_to(
        recipient=recipient_email,
        subject="[TradingBoard.ai] Your rank just moved up",
        body_text=body,
    )


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
    if action not in ("earlyaccess", "contact", "validation", "applicants", "participants", "myposition", "unsubscribe", "verify", "send_bumped_digest"):
        return json_response({"error": "Invalid or missing action"}, 400)

    if action == "earlyaccess":
        email = (payload.get("email") or "").strip()
        source = (payload.get("source") or "M1").strip().upper()
        if source not in ("M1", "M2", "EA"):
            source = "M1"
        ref_code_in = (payload.get("ref") or "").strip().lower()
        if not email or not EMAIL_RE.match(email):
            return json_response({"error": "Valid email is required"}, 400)
        try:
            participant, created = get_or_create_participant(email, source=source)
            # Mark as early-access participant for gamification
            db = get_db()
            try:
                if created:
                    db.collection("participants").document(email).set(
                        {
                            "earlyAccessOptIn": True,
                            # New participants must confirm their email via verification link.
                            "emailVerified": False,
                        },
                        merge=True,
                    )
                else:
                    db.collection("participants").document(email).set(
                        {
                            "earlyAccessOptIn": True,
                        },
                        merge=True,
                    )
            except Exception:
                pass

            # Record referral relationship for early-access signups, but do NOT credit the referrer yet.
            # Referral credit (incrementing referrer's "referrals" count) happens only after the new user
            # confirms their email via the verification link.
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
                        # Store who invited this participant and that referral has not been credited yet
                        db.collection("participants").document(email).set(
                            {
                                "referredByEmail": referrer_email,
                                "referredByRef": ref_code_in,
                                "referralCredited": False,
                            },
                            merge=True,
                        )
                        break
                except Exception:
                    pass
            if not created:
                # Duplicate: ignore, do not overwrite or send link again
                return json_response({"success": True}, 200)
            # New participant: notify owner and send user their referral link
            try:
                owner_recipient = sanitize_email(get_config("TO_EMAIL") or "info@tradingboard.ai") or "info@tradingboard.ai"
                send_email_to(
                    recipient=owner_recipient,
                    subject="[TradingBoard.ai] Early access request",
                    body_text=f"Early access signup (source {source}):\n\nEmail: {email}",
                )
            except Exception:
                # Owner notification is best-effort; do not block user email.
                pass
            ref_code = participant.get("refCode") or ""
            invite_link = f"https://tradingboard.ai/?ref={ref_code}#early_access"
            verify_link = f"https://tradingboard.ai/?ref={ref_code}#verify"
            ranks = compute_ranks()
            total = len(ranks) or 1
            rank = ranks.get(email)
            rank_label = f"#{rank}" if rank else "#—"
            tier_display = tier_display_name(rank)
            reward = desired_reward(rank)
            myposition_link = f"https://tradingboard.ai/?ref={ref_code}#myposition"
            reward_tiers_block = (
                "The Reward Tiers:\n"
                "    - ALL: Access to the Private Betas. Be the first to trade on the engine.\n"
                "    - TOP 100: Founding Member Status + 50% Discount (3 years).\n"
                "    - TOP 50: Founding Member Status + 75% Discount (3 years).\n"
                "    - TOP 25: Founding Member Status + 75% Discount (3 years) + 1,000 Compute Credits.\n"
                "    - TOP 10: Founding Member Status + 75% Discount (3 years) + 10,000 Compute Credits.\n"
                "    - TOP 5 (Gold): Founding Member Status + 100% Discount (3 years) + 10,000 Compute Credits.\n"
                "    - TOP 1 (Platinum): Founding Member Status + 100% Discount (10 YEARS) + 50,000 Compute Credits.\n"
            )
            welcome_body = (
                f"You're in! Your TradingBoard.ai Rank: {rank_label}\n\n"
                "Thank you for requesting early access to TradingBoard.ai.\n\n"
                f"Confirm your email {verify_link}\n\n"
                f"Your Current Status: Rank {rank_label}\n"
                f"Current Standing: {tier_display}\n\n"
                "Don't get bumped! Rankings are live. If others refer more members, your rank will drop. "
                "To climb the leaderboard and protect your tier, invite another expert to help us validate the product.\n\n"
                "Jump back up:\n"
                "Simply share your unique invite link with a colleague. Once they validate the product, you'll leapfrog back toward the top. "
                "More referrals = better position. Rank is determined primarily by your number of successful referrals; "
                "the date you joined only serves as a tie-breaker for members with the same referral count.\n\n"
                f"Your Unique Invite Link: 👉 {invite_link}\n\n"
                f"Track your live position here: {myposition_link}\n\n"
                "Please note: The final ranking will be frozen at the moment of the official beta release.\n\n"
                f"As a member of the {tier_display}, your current reward is: {reward}\n\n"
                f"{reward_tiers_block}\n"
                "Best regards,\n\n"
                "Rehor Vykoupil\n"
                "CEO • tradingboard.ai\n"
            )
            send_email_to(
                recipient=email,
                subject=f"[TradingBoard.ai] You're in! Your Rank: {rank_label}",
                body_text=welcome_body,
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
        early_access_opt_in = bool(payload.get("earlyAccessOptIn"))  # checkbox from validation form
        if not email or not EMAIL_RE.match(email):
            return json_response({"error": "Valid email is required"}, 400)

        # Create participant only if new (source V1). Duplicate = ignore, no overwrite.
        participant, created = get_or_create_participant(
            email, source="V1", notes=notes or None, name=name or None, surname=surname or None, is_mentor=is_mentor
        )
        db = get_db()

        doc_ref = db.collection("participants").document(email)
        # Early access opt-in from validation: only ever turns it on, never off.
        # For brand-new participants, explicit False should be stored so they are excluded from gamification.
        try:
            if created:
                if early_access_opt_in:
                    doc_ref.set({"earlyAccessOptIn": True}, merge=True)
                else:
                    doc_ref.set({"earlyAccessOptIn": False}, merge=True)
            else:
                if early_access_opt_in:
                    doc_ref.set({"earlyAccessOptIn": True}, merge=True)
        except Exception:
            pass

        # Always update notes and validationSubmittedAt (latest answers win)
        try:
            to_set = {"validationSubmittedAt": firestore.SERVER_TIMESTAMP}
            if notes:
                to_set["notes"] = notes
            doc_ref.set(to_set, merge=True)
        except Exception:
            pass

        # Handle referral: only if a valid ref code is present AND this is a new participant (first-time submit).
        # We record who invited this participant, but do NOT credit the referrer yet. Credit is applied only
        # after this participant confirms their email via the verification link.
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
                    db.collection("participants").document(email).set(
                        {
                            "referredByEmail": referrer_email,
                            "referredByRef": ref_code_in,
                            "referralCredited": False,
                        },
                        merge=True,
                    )
                    break
            except Exception:
                pass

        # If not opted into early access, store feedback only and skip gamification (no ranks, no referrals, no bumped).
        # Determine effective early-access flag from participant plus incoming checkbox.
        is_early = bool(participant.get("earlyAccessOptIn")) or early_access_opt_in

        if not is_early:
            # Owner email with full notes, simple thank-you to user (no rank/tier/rewards).
            body_lines = [
                "Validation-only submission (no early access):",
                f"Email: {email}",
            ]
            if notes:
                body_lines.append("")
                body_lines.append("User notes:")
                body_lines.append(notes)
            body = "\n".join(body_lines)
            try:
                owner_recipient = sanitize_email(get_config("TO_EMAIL") or "info@tradingboard.ai") or "info@tradingboard.ai"
                send_email_to(
                    recipient=owner_recipient,
                    subject="[TradingBoard.ai] Validation feedback (non-early-access)",
                    body_text=body,
                )
                # Optional simple thank-you email
                thanks_lines = [
                    "Thank you for sharing your feedback with the TradingBoard.ai team.",
                    "",
                    "Your answers help us shape the product, but you have chosen not to join the early access leaderboard.",
                    "You will not receive rank or referral-based updates, and you are not included in the rewards tiers.",
                ]
                if notes:
                    thanks_lines.append("")
                    thanks_lines.append("Your submitted answers:")
                    thanks_lines.append(notes)
                thanks_lines.append("")
                thanks_lines.append("Best regards,")
                thanks_lines.append("")
                thanks_lines.append("Rehor Vykoupil")
                thanks_lines.append("CEO • tradingboard.ai")
                send_email_to(
                    recipient=email,
                    subject="[TradingBoard.ai] Thanks for your feedback",
                    body_text="\n".join(thanks_lines),
                )
            except Exception:
                pass
            return json_response({"success": True}, 200)

        # Compute ranks after potential referral update (only for early-access participants)
        ranks = compute_ranks()
        total_participants = len(ranks) or 1
        user_rank = ranks.get(email)

        # Build invite link using user's personal ref code
        user_ref_code = participant.get("refCode") or generate_ref_code()
        invite_link = f"https://tradingboard.ai/?ref={user_ref_code}#early_access"
        myposition_link = f"https://tradingboard.ai/?ref={user_ref_code}#myposition"
        verify_link = f"https://tradingboard.ai/?ref={user_ref_code}#verify"

        # Early-access "you're in" rank email
        rank_label = f"#{user_rank}" if user_rank else "#—"
        user_tier_display = tier_display_name(user_rank)
        user_reward = desired_reward(user_rank)
        reward_tiers_block = (
            "The Reward Tiers:\n"
            "    - ALL: Access to the Private Betas. Be the first to trade on the engine.\n"
            "    - TOP 100: Founding Member Status + 50% Discount (3 years).\n"
            "    - TOP 50: Founding Member Status + 75% Discount (3 years).\n"
            "    - TOP 25: Founding Member Status + 75% Discount (3 years) + 1,000 Compute Credits.\n"
            "    - TOP 10: Founding Member Status + 75% Discount (3 years) + 10,000 Compute Credits.\n"
            "    - TOP 5 (Gold): Founding Member Status + 100% Discount (3 years) + 10,000 Compute Credits.\n"
            "    - TOP 1 (Platinum): Founding Member Status + 100% Discount (10 YEARS) + 50,000 Compute Credits.\n"
        )
        rank_email_subject = f"[TradingBoard.ai] You're in! Your Rank: {rank_label}"
        rank_email_body = f"""You're in! Your TradingBoard.ai Rank: {rank_label}

Thank you for requesting early access to TradingBoard.ai.

Confirm your email {verify_link}

Your Current Status: Rank {rank_label}
Current Standing: {user_tier_display}

Don't get bumped! Rankings are live. If others refer more members, your rank will drop. To climb the leaderboard and protect your tier, invite another expert to help us validate the product.

Jump back up:
Simply share your unique invite link with a colleague. Once they validate the product, you'll leapfrog back toward the top. More referrals = better position. Rank is determined primarily by your number of successful referrals; the date you joined only serves as a tie-breaker for members with the same referral count.

Your Unique Invite Link: 👉 {invite_link}

Track your live position here: {myposition_link}

Please note: The final ranking will be frozen at the moment of the official beta release.

As a member of the {user_tier_display}, your current reward is: {user_reward}.

{reward_tiers_block}

Best regards,

Rehor Vykoupil
CEO • tradingboard.ai
"""

        # Feedback-only email (no rank in subject/body) for the same submission
        feedback_subject = "[TradingBoard.ai] Thanks for your submission"
        submitted_answers = notes or "(none)"
        feedback_body = f"""Thank you for your submission to TradingBoard.ai.

Your feedback helps us shape the product.

Your submitted answers:
{submitted_answers}

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
        body_lines.append(feedback_body)
        body = "\n".join(body_lines)

        try:
            # Always notify site owner of validation submission
            owner_recipient2 = sanitize_email(get_config("TO_EMAIL") or "info@tradingboard.ai") or "info@tradingboard.ai"
            send_email_to(
                recipient=owner_recipient2,
                subject="[TradingBoard.ai] Validation feedback",
                body_text=body,
            )
            # Send both rank email and feedback email to user for early-access validation submissions
            send_email_to(recipient=email, subject=rank_email_subject, body_text=rank_email_body)
            send_email_to(recipient=email, subject=feedback_subject, body_text=feedback_body)

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
            if d.get("earlyAccessOptIn") is False:
                return json_response({"error": "This participant is not part of the early access leaderboard"}, 403)
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
                # Annotate details with early-access information for admin view
                ea_flag = d.get("earlyAccessOptIn")
                if ea_flag is True:
                    prefix = "Early Access: requested\n"
                    notes = prefix + (notes or "")
                elif ea_flag is False:
                    prefix = "Early Access: not requested\n"
                    notes = prefix + (notes or "")
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
                t = d.get("createdAt")
                entry_at = t.isoformat() if (t and hasattr(t, "isoformat")) else None
                if not entry_at:
                    t = d.get("validationSubmittedAt")
                    entry_at = t.isoformat() if (t and hasattr(t, "isoformat")) else None
                t_sub = d.get("validationSubmittedAt")
                submitted_at = t_sub.isoformat() if (t_sub and hasattr(t_sub, "isoformat")) else None
                referred_by_email = d.get("referredByEmail") or ""
                email_verified = bool(d.get("emailVerified"))
                referred_by_ref = d.get("referredByRef") or ""
                applicants.append({
                    "order": idx,
                    "email": email,
                    "source": source,
                    "name": name,
                    "surname": surname,
                    "notes": notes,
                    "entryAt": entry_at,
                    "submittedAt": submitted_at,
                    "referrals": refs,
                    "referredByEmail": referred_by_email,
                    "referredByRef": referred_by_ref,
                    "emailVerified": email_verified,
                })
        except Exception:
            applicants = []

        return json_response({"applicants": applicants}, 200)

    if action == "participants":
        db = get_db()
        participants_out = []
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
                full_email = d.get("email") or snap.id
                source = d.get("source") or ""
                notes = d.get("notes") or ""
                name = d.get("name") or ""
                surname = d.get("surname") or ""
                refs = int(d.get("referrals") or 0)

                # Annotate early access flag in notes
                ea_flag = d.get("earlyAccessOptIn")
                if ea_flag is True:
                    prefix = "Early Access: requested\n"
                    notes = prefix + (notes or "")
                elif ea_flag is False:
                    prefix = "Early Access: not requested\n"
                    notes = prefix + (notes or "")

                # Fallback name parsing from notes (legacy)
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

                # Redact any email-looking text in notes
                try:
                    notes_redacted = EMAIL_EXTRACT_RE.sub("***", notes)
                except Exception:
                    notes_redacted = notes

                t = d.get("createdAt")
                entry_at = t.isoformat() if (t and hasattr(t, "isoformat")) else None
                if not entry_at:
                    t = d.get("validationSubmittedAt")
                    entry_at = t.isoformat() if (t and hasattr(t, "isoformat")) else None
                t_sub = d.get("validationSubmittedAt")
                submitted_at = t_sub.isoformat() if (t_sub and hasattr(t_sub, "isoformat")) else None
                ref_by_email_full = d.get("referredByEmail") or ""
                referred_by_email = mask_email_for_public(ref_by_email_full)
                referred_by_ref = d.get("referredByRef") or ""

                participants_out.append({
                    "order": idx,
                    "email": mask_email_for_public(full_email),
                    "source": source,
                    "name": name,
                    "surname": surname,
                    "notes": notes_redacted,
                    "entryAt": entry_at,
                    "submittedAt": submitted_at,
                    "referrals": refs,
                    "referredByEmail": referred_by_email,
                    "referredByRef": referred_by_ref,
                })
        except Exception:
            participants_out = []

        return json_response({"applicants": participants_out}, 200)

    if action == "verify":
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
            already_verified = bool(d.get("emailVerified"))

            # Ranks before any referral credit is applied
            ranks_before = compute_ranks()

            # Mark email as verified
            snap.reference.set({"emailVerified": True}, merge=True)

            # If this participant was referred by someone and that referral has not yet been credited,
            # increment the referrer's referrals count and mark this referral as credited.
            referred_by_email = (d.get("referredByEmail") or "").strip()
            referral_credited = bool(d.get("referralCredited"))
            referrer_email = None
            if referred_by_email and not referral_credited:
                ref_doc = db.collection("participants").document(referred_by_email)
                ref_snap = ref_doc.get()
                if ref_snap.exists:
                    ref_data = ref_snap.to_dict() or {}
                    current_refs = int(ref_data.get("referrals") or 0)
                    ref_doc.update({"referrals": current_refs + 1})
                    snap.reference.set({"referralCredited": True}, merge=True)
                    referrer_email = referred_by_email

            # If we just credited a referrer, enqueue daily standing notifications
            # for the referrer (moved up) and any participants whose rank worsened ("bumped").
            if referrer_email:
                try:
                    # Ranks after referral credit
                    ranks_after = compute_ranks()

                    # --- Referrer "rank moved up" pending notification ---
                    ref_old_rank = ranks_before.get(referrer_email) or 0
                    ref_new_rank = ranks_after.get(referrer_email) or 0
                    if ref_old_rank and ref_new_rank and ref_new_rank != ref_old_rank:
                        db.collection("bumped_pending").document(referrer_email).set(
                            {
                                "oldRank": ref_old_rank,
                                "newRank": ref_new_rank,
                                "detectedAt": firestore.SERVER_TIMESTAMP,
                                "kind": "up",
                            },
                            merge=True,
                        )

                    # --- "You've been bumped" pending notifications ---
                    bumped = {}
                    for em, old_rank in ranks_before.items():
                        new_rank = ranks_after.get(em)
                        if new_rank is not None and new_rank > old_rank:
                            bumped[em] = (old_rank, new_rank)

                    for bumped_email, (old_rank, new_rank) in bumped.items():
                        try:
                            snap_b = db.collection("participants").document(bumped_email).get()
                            if not snap_b.exists:
                                continue
                            d_b = snap_b.to_dict() or {}
                            if d_b.get("unsubscribedFromRankAlerts"):
                                continue
                            ref_code_b = (d_b.get("refCode") or "").strip()
                            if not ref_code_b:
                                continue
                            first_name = (d_b.get("name") or "").strip() or "there"
                            db.collection("bumped_pending").document(bumped_email).set(
                                {
                                    "oldRank": old_rank,
                                    "newRank": new_rank,
                                    "detectedAt": firestore.SERVER_TIMESTAMP,
                                    "kind": "down",
                                    "firstName": first_name,
                                },
                                merge=True,
                            )
                        except Exception:
                            pass
                except Exception:
                    # Best-effort only; verification itself should still succeed.
                    pass

            return json_response({"success": True, "alreadyVerified": already_verified}, 200)
        except Exception as e:
            return json_response({"error": "Server error", "detail": str(e)}, 500)

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
        # Called by Cloud Scheduler periodically. Sends one standing email per pending user,
        # but only when current UTC time matches STANDING_EMAIL_TIME (or 14:00 GMT by default).
        db = get_db()
        try:
            # Determine if it's time to send the daily standing digest
            time_str = get_config("STANDING_EMAIL_TIME") or DEFAULT_STANDING_EMAIL_TIME
            try:
                hour_s, minute_s = time_str.split(":", 1)
                target_hour = int(hour_s)
                target_minute = int(minute_s)
            except Exception:
                target_hour = 14
                target_minute = 0

            tz = None
            if ZoneInfo is not None:
                try:
                    tz = ZoneInfo(STANDING_EMAIL_TIMEZONE)
                except Exception:
                    tz = None
            if tz is None:
                tz = datetime.timezone.utc

            now = datetime.datetime.now(tz)
            if now.hour != target_hour or now.minute != target_minute:
                # Not the configured send time – exit without sending.
                return json_response({"success": True, "skipped": True}, 200)

            pending = list(db.collection("bumped_pending").stream())
            for snap in pending:
                bumped_email = snap.id
                d = snap.to_dict() or {}
                old_rank = int(d.get("oldRank") or 0)
                new_rank = int(d.get("newRank") or 0)
                kind = (d.get("kind") or "down").lower()
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
                first_name = (part.get("name") or "").strip() or d.get("firstName") or "there"
                try:
                    if kind == "up":
                        send_rank_up_email(
                            recipient_email=bumped_email,
                            new_rank=current_rank,
                            ref_code=ref_code_b,
                        )
                    else:
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
