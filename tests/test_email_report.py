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


def test_email_carries_the_essentials(tmp_path):
    import json

    from trend_bot import db

    con = db.connect(tmp_path / "m.db")
    today = dt.date.today()
    recent = (today - dt.timedelta(days=5)).isoformat()
    con.executemany("INSERT INTO tickers(ticker, cik) VALUES (?, ?)", [("AAA", 1), ("BBB", 2)])
    con.executemany("INSERT INTO prices(ticker, date, close) VALUES (?, ?, ?)",
                    [("AAA", recent, 110.0), ("BBB", recent, 45.0)])
    con.executemany("INSERT INTO ideas_holdings VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [("AAA", 1, "2026-09-30", 100.0, 0.9, "Technology", "cheap"),
                     ("BBB", 2, "2026-09-30", 50.0, 0.8, "Energy", "momentum")])
    con.execute("INSERT INTO events VALUES ('acc1', 2, '8-K', ?, '4.02')", (recent,))
    db.set_meta(con, "ideas_portfolio", json.dumps({
        "date": recent, "buys": [{"ticker": "AAA", "why": "cheap vs sales"}],
        "sells": [{"ticker": "OLD", "since": "2026-08-31", "return": -0.04}], "holds": []}))
    con.commit()
    sections = dict(email_report.digest(con, ["Prices are 3 trading days old."]))
    changes = next(v for k, v in sections.items() if k.startswith("This month's changes"))
    assert changes == ["Buy AAA (cheap vs sales)", "Sell OLD (-4.0% since 2026-08-31)"]
    table = next(v for k, v in sections.items() if k.startswith("The portfolio to follow"))
    assert table[0][0] == "Stock" and table[1] == ["AAA", "2026-09-30", "100.00", "110.00", "+10.0%"]
    flags = next(v for k, v in sections.items() if k.startswith("🚩"))
    assert flags[0].startswith("BBB") and "can't be relied on" in flags[0]
    assert sections["Bot health"] == ["Prices are 3 trading days old."]

    cfg = {"EMAIL_USER": "me@gmail.com", "EMAIL_APP_PASSWORD": "x", "EMAIL_TO": "me@gmail.com"}
    msg = email_report.build(cfg, email_report.digest(con, []), None)
    text = msg.get_body(("plain",)).get_content()
    html = msg.get_body(("html",)).get_content()
    assert "AAA  2026-09-30  100.00  110.00  +10.0%" in text and "All fine." in text
    assert "<table" in html and "<td" in html and "+10.0%" in html and "BBB (" in html
