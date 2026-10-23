import datetime as dt

from trend_bot import email_report


def test_not_set_up_means_nothing_happens(tmp_path, monkeypatch, capsys):
    from trend_bot.cli import main

    for k in ("EMAIL_USER", "EMAIL_APP_PASSWORD", "EMAIL_TO"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.chdir(tmp_path)
    assert email_report.settings() is None
    assert main(["send-email", "--db", str(tmp_path / "none.db")]) == 0
    assert "isn't set up" in capsys.readouterr().out


def test_settings_from_env_or_dotenv(tmp_path, monkeypatch):
    monkeypatch.delenv("EMAIL_TO", raising=False)
    monkeypatch.setenv("EMAIL_USER", "me@gmail.com")
    monkeypatch.setenv("EMAIL_APP_PASSWORD", "abcd efgh ijkl mnop")
    cfg = email_report.settings(tmp_path / ".env")
    assert cfg["EMAIL_APP_PASSWORD"] == "abcdefghijklmnop" and cfg["EMAIL_TO"] == "me@gmail.com"
    (tmp_path / ".env").write_text("EMAIL_TO=you@example.com\n")
    assert email_report.settings(tmp_path / ".env")["EMAIL_TO"] == "you@example.com"


def test_email_has_summary_and_report(tmp_path, monkeypatch):
    report = tmp_path / "latest.html"
    report.write_text("<html>report</html>")
    cfg = {"EMAIL_USER": "me@gmail.com", "EMAIL_APP_PASSWORD": "x", "EMAIL_TO": "you@example.com"}
    msg = email_report.build(cfg, ["📈 **Since 2026-09-30**: portfolio **+1.0%**", ""], report, dt.date(2026, 10, 23))
    assert msg["Subject"] == "Trend Bot daily report, Fri 23 Oct 2026" and msg["To"] == "you@example.com"
    text = msg.get_body(("plain",)).get_content()
    assert "Since 2026-09-30: portfolio +1.0%" in text and "**" not in text
    (att,) = list(msg.iter_attachments())
    assert att.get_filename() == "report-2026-10-23.html" and b"report" in att.get_payload(decode=True)

    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, context=None, timeout=None):
            sent["host"] = (host, port)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def login(self, user, password):
            sent["login"] = (user, password)

        def send_message(self, m):
            sent["subject"] = m["Subject"]

    monkeypatch.setattr(email_report.smtplib, "SMTP_SSL", FakeSMTP)
    email_report.send(cfg, msg)
    assert sent == {"host": ("smtp.gmail.com", 465), "login": ("me@gmail.com", "x"),
                    "subject": "Trend Bot daily report, Fri 23 Oct 2026"}
