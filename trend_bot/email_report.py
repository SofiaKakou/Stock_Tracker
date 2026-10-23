"""The daily report by email (optional), through a Gmail account.

Free: Gmail lets programs send mail with an "app password" (Google Account -> Security ->
2-Step Verification on -> App passwords). Set these as GitHub secrets (or in .env):

  EMAIL_USER          the Gmail address that sends (e.g. yourname@gmail.com)
  EMAIL_APP_PASSWORD  the 16-letter app password (not your normal password)
  EMAIL_TO            where to send it (optional; default: EMAIL_USER itself)

Without EMAIL_USER and EMAIL_APP_PASSWORD nothing is sent and nothing fails.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

SMTP_HOST, SMTP_PORT = "smtp.gmail.com", 465


def settings(env_file: str | Path = ".env") -> dict | None:
    """EMAIL_* from the environment (GitHub secrets) or .env; None when email isn't set up."""
    found = {k: os.environ.get(k, "").strip() for k in ("EMAIL_USER", "EMAIL_APP_PASSWORD", "EMAIL_TO")}
    if Path(env_file).exists():
        for line in Path(env_file).read_text(encoding="utf-8-sig").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() in found and not found[key.strip()]:
                found[key.strip()] = value.strip().strip('"').strip("'")
    if not (found["EMAIL_USER"] and found["EMAIL_APP_PASSWORD"]):
        return None
    found["EMAIL_APP_PASSWORD"] = found["EMAIL_APP_PASSWORD"].replace(" ", "")   # Google shows it in 4 groups
    found["EMAIL_TO"] = found["EMAIL_TO"] or found["EMAIL_USER"]
    return found


def plain(text: str) -> str:
    """Discord-style **bold** into plain text."""
    return re.sub(r"\*\*(.+?)\*\*", r"\1", text)


def build(cfg: dict, summary: list[str], report: Path | None, today: dt.date | None = None) -> EmailMessage:
    today = today or dt.date.today()
    msg = EmailMessage()
    msg["Subject"] = f"Trend Bot daily report, {today:%a %d %b %Y}"
    msg["From"], msg["To"] = cfg["EMAIL_USER"], cfg["EMAIL_TO"]
    body = [plain(line) for line in summary if line]
    body.append("The full report is attached: open it in your browser (charts included)." if report
                else "No report file was made tonight.")
    body.append("\nPast patterns, not certainties; not financial advice.")
    msg.set_content("\n".join(body))
    if report:
        msg.add_attachment(report.read_bytes(), maintype="text", subtype="html", filename=f"report-{today}.html")
    return msg


def send(cfg: dict, msg: EmailMessage, timeout: float = 30) -> None:
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ssl.create_default_context(), timeout=timeout) as s:
        s.login(cfg["EMAIL_USER"], cfg["EMAIL_APP_PASSWORD"])
        s.send_message(msg)
