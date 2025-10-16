import aiosmtplib, os, logging
from email.message import EmailMessage
from datetime import datetime, timezone

log = logging.getLogger(__name__)

SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
MAIL_FROM = os.getenv("MAIL_FROM", SMTP_USER or "noreply@example.com")
MAIL_TO   = os.getenv("MAIL_TO", "johannes.dachs@gmx.de")

async def send_mail(subject: str, body: str):
    if not SMTP_HOST or not MAIL_TO:
        log.warning("SMTP not configured; skipping email: %s", subject)
        return
    msg = EmailMessage()
    msg["From"] = MAIL_FROM
    msg["To"] = MAIL_TO
    msg["Subject"] = subject
    msg.set_content(body)
    await aiosmtplib.send(
        msg, hostname=SMTP_HOST, port=SMTP_PORT,
        username=SMTP_USER, password=SMTP_PASS, start_tls=True, timeout=10
    )
    log.info("Email sent: %s", subject)

def fmt_ts(ts=None):
    ts = ts or datetime.now(timezone.utc)
    return ts.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
