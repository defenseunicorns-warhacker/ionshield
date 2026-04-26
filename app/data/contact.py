"""
Contact form persistence and email delivery.

Design decisions
----------------
* Submissions are ALWAYS saved to the DB first. Email is best-effort on top.
* Raw client IPs are never stored — we hash with SHA-256 for abuse detection.
* Honeypot submissions are saved with status="spam" and silently accepted.
* SMTP is optional: if settings.smtp_host is empty the email step is skipped
  and email_sent stays 0. No exception is raised to the caller.

SMTP providers
--------------
Any STARTTLS-capable SMTP relay works:
  SendGrid : smtp.sendgrid.net:587          (username="apikey", password=SG.key)
  SES SMTP : email-smtp.<region>.amazonaws.com:587
  Mailgun  : smtp.mailgun.org:587
  Gmail    : smtp.gmail.com:587             (use App Password)
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from email.message import EmailMessage

logger = logging.getLogger(__name__)


# ── IP hashing ────────────────────────────────────────────────────────────────


def _hash_ip(ip: str) -> str:
    """One-way SHA-256 hash of a raw IP address string."""
    return hashlib.sha256(ip.encode()).hexdigest()


# ── DB save ───────────────────────────────────────────────────────────────────


async def save_inquiry(
    *,
    org: str,
    email: str,
    sector: str,
    interest: str,
    client_ip: str,
    status: str = "new",
) -> int:
    """
    Persist a pilot inquiry row.  Returns the new row ID.
    """
    from app.data.db import get_engine, pilot_inquiries  # late import avoids circular

    engine = get_engine()
    async with engine.begin() as conn:
        result = await conn.execute(
            pilot_inquiries.insert().values(
                created_at=datetime.now(timezone.utc),
                org=org[:500],
                email=email[:254],
                sector=sector[:100],
                interest=interest[:4000],
                ip_hash=_hash_ip(client_ip),
                email_sent=0,
                status=status,
            )
        )
        return result.inserted_primary_key[0]


async def mark_email_sent(row_id: int) -> None:
    """Flip email_sent = 1 after successful SMTP delivery."""
    from app.data.db import get_engine, pilot_inquiries

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.execute(
            pilot_inquiries.update()
            .where(pilot_inquiries.c.id == row_id)
            .values(email_sent=1)
        )


async def list_inquiries(limit: int = 50, offset: int = 0) -> list[dict]:
    """Return recent submissions for the admin view, newest first."""
    from sqlalchemy import select
    from app.data.db import get_engine, pilot_inquiries

    engine = get_engine()
    async with engine.begin() as conn:
        result = await conn.execute(
            select(pilot_inquiries)
            .order_by(pilot_inquiries.c.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        rows = result.mappings().all()

    return [
        {
            "id": r["id"],
            "created_at": (
                r["created_at"].isoformat()
                if hasattr(r["created_at"], "isoformat")
                else str(r["created_at"])
            ),
            "org": r["org"],
            "email": r["email"],
            "sector": r["sector"],
            "interest": r["interest"],
            "email_sent": bool(r["email_sent"]),
            "status": r["status"],
            # ip_hash intentionally excluded from API responses
        }
        for r in rows
    ]


async def count_inquiries() -> int:
    """Total submission count (for pagination)."""
    from sqlalchemy import func, select
    from app.data.db import get_engine, pilot_inquiries

    engine = get_engine()
    async with engine.begin() as conn:
        result = await conn.execute(select(func.count()).select_from(pilot_inquiries))
        return result.scalar() or 0


# ── Email delivery ────────────────────────────────────────────────────────────


async def send_inquiry_email(
    *,
    org: str,
    email: str,
    sector: str,
    interest: str,
    submitted_at: datetime,
) -> bool:
    """
    Send a notification email via SMTP.
    Returns True on success, False on failure (caller decides whether to log).
    No exceptions are propagated — email failures are non-fatal.
    """
    from app.config import settings

    if not settings.smtp_enabled:
        return False

    try:
        import aiosmtplib

        msg = EmailMessage()
        msg["Subject"] = f"[IonShield Pilot Inquiry] {org}"
        msg["From"] = settings.smtp_from_email
        msg["To"] = settings.contact_to_email
        msg["Reply-To"] = email

        body = (
            f"New pilot inquiry received via IonShield.io\n"
            f"{'─' * 48}\n\n"
            f"Organization : {org}\n"
            f"Sector       : {sector}\n"
            f"Email        : {email}\n"
            f"Submitted    : {submitted_at.strftime('%Y-%m-%d %H:%M:%S UTC')}\n\n"
            f"Message\n{'─' * 48}\n"
            f"{interest or '(no message)'}\n\n"
            f"{'─' * 48}\n"
            f"Reply directly to this email to respond to the inquirer.\n"
            f"Submissions are also stored in the ionshield.db database.\n"
        )
        msg.set_content(body)

        await aiosmtplib.send(
            msg,
            hostname=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_username,
            password=settings.smtp_password.get_secret_value(),
            start_tls=settings.smtp_tls,
            timeout=10,
        )
        return True

    except Exception as exc:  # noqa: BLE001
        logger.warning("Contact email delivery failed: %s", exc)
        return False
