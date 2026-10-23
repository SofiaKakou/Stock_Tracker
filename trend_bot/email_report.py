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


def digest(con, problems: list[str], flag_days: int = 30, change_days: int = 35) -> list[tuple[str, list]]:
    """The essentials for the email, as (heading, lines) sections; a line is text or a table row (list)."""
    import pandas as pd

    from trend_bot import events, ideas, paper

    out = []
    live = ideas.live_line(con)
    if live:
        demo = paper.line(con)
        out.append(("How the portfolio is really doing", [plain(live).removeprefix("📈 ")]
                    + ([plain(demo).removeprefix("🧪 ")] if demo else [])))
    port = ideas.load_portfolio(con)
    if port and (dt.date.today() - dt.date.fromisoformat(port["date"][:10])).days <= change_days:
        lines = [f"Buy {b['ticker']} ({b.get('why') or 'top ranked'})" for b in port["buys"]]
        lines += [f"Sell {s['ticker']}" + (f" ({s['return']:+.1%} since {s['since']})" if s.get("return") is not None
                                          else "") for s in port["sells"]]
        out.append((f"This month's changes ({port['date'][:10]})", lines or ["No changes: keep everything."]))
    held = pd.read_sql_query("SELECT ticker, since, entry_price FROM ideas_holdings ORDER BY rank", con)
    if len(held):
        rows = [["Stock", "Held since", "Bought at", "Price now", "Gain or loss"]]
        for h in held.itertuples(index=False):
            last = con.execute("SELECT close FROM prices WHERE ticker = ? ORDER BY date DESC LIMIT 1",
                               (h.ticker,)).fetchone()
            now = last[0] if last else None
            ret = now / h.entry_price - 1 if now and h.entry_price else None
            rows.append([h.ticker, h.since, f"{h.entry_price:,.2f}" if h.entry_price else "-",
                         f"{now:,.2f}" if now else "-", f"{ret:+.1%}" if ret is not None else "-"])
        out.append((f"The portfolio to follow ({len(held)} stocks, equal amounts in each)", rows))
        cik_of = dict(con.execute(f"SELECT cik, ticker FROM tickers WHERE cik IS NOT NULL AND ticker IN "
                                  f"({','.join('?' * len(held))})", list(held["ticker"])).fetchall())
        flags = events.recent_flags(con, list(cik_of), days=flag_days)
        out.append((f"🚩 Red flags in these stocks (last {flag_days} days)",
                    [f"{cik_of[r.cik]} ({r.filed}): {events.describe(r.items)}" for r in flags.itertuples(index=False)]
                    or ["None."]))
    note = ideas.backtest_note(con)
    if note:
        out.append(("What 20 years of testing says", [note, *ideas.bad_times(con)]))
    out.append(("Bot health", problems or ["All fine."]))
    return [("In short", in_short(out, problems))] + out


def in_short(sections, problems: list[str]) -> list[str]:
    """Two or three plain sentences: what to do, anything to look at, and whether the bot is OK."""
    got = dict(sections)
    todo = next((v for k, v in got.items() if k.startswith("This month's changes")), None)
    if todo is None:
        lines = ["Nothing to do today: the portfolio only changes once a month."]
    elif todo == ["No changes: keep everything."]:
        lines = ["No changes to the portfolio this month: keep everything."]
    else:
        buys = sum(x.startswith("Buy ") for x in todo)
        sells = sum(x.startswith("Sell ") for x in todo)
        lines = [f"This month the portfolio sells {sells} and buys {buys} stock{'s' if buys != 1 else ''} "
                 "(listed below), if you follow it."]
    flags = next((v for k, v in got.items() if k.startswith("🚩")), ["None."])
    if flags != ["None."]:
        lines.append(f"🚩 {len(flags)} warning sign{'s' if len(flags) != 1 else ''} in your stocks: worth a look "
                     "before buying more.")
    lines.append("The bot is working normally." if not problems else
                 "The bot had a problem tonight (details under Bot health in the email and Discord); it usually fixes itself.")
    return lines


def _text(sections) -> str:
    parts = []
    for title, lines in sections:
        rows = [("  ".join(str(c) for c in x) if isinstance(x, list) else x) for x in lines]
        parts.append(title + "\n" + "\n".join(f"  {r}" for r in rows))
    return "\n\n".join(parts)


def _html(sections, footer: list[str], chart_title: str | None = None, demo_chart: bool = False) -> str:
    from html import escape

    cell = "padding:3px 10px 3px 0;text-align:left;border-bottom:1px solid #eee"
    out = ['<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;font-size:15px;color:#222;'
           'max-width:640px">']
    for k, (title, lines) in enumerate(sections):
        if k == 1 and chart_title:
            out.append(f'<h3 style="margin:18px 0 6px">{escape(chart_title)}</h3>'
                       '<img src="cid:growth" alt="Growth chart" style="width:100%;max-width:640px;height:auto">'
                       '<p style="color:#777;font-size:13px;margin:4px 0">A test on past data, after trading '
                       "costs. The real record is the line below.</p>")
        out.append(f'<h3 style="margin:18px 0 6px">{escape(title)}</h3>')
        if k == 0 and demo_chart:
            out.append('<img src="cid:demo" alt="Demo accounts vs the S&amp;P 500" '
                       'style="width:100%;max-width:640px;height:auto">')
        table = [x for x in lines if isinstance(x, list)]
        text = [x for x in lines if not isinstance(x, list)]
        if text:
            out.append("<ul style=\"margin:0;padding-left:18px\">" +
                       "".join(f"<li>{escape(t)}</li>" for t in text) + "</ul>")
        if table:
            head, *body = table
            out.append('<table style="border-collapse:collapse;font-size:14px">'
                       + "<tr>" + "".join(f'<th style="{cell}">{escape(h)}</th>' for h in head) + "</tr>"
                       + "".join("<tr>" + "".join(f'<td style="{cell}">{escape(str(c))}</td>' for c in r) + "</tr>"
                                 for r in body) + "</table>")
    out += [f'<p style="color:#777;font-size:13px;margin-top:18px">{escape(f)}</p>' for f in footer]
    return "".join(out) + "</div>"


def build(cfg: dict, summary, report: Path | None, today: dt.date | None = None,
          chart: bytes | None = None, chart_title: str = "What $10,000 would have become in testing",
          demo_chart: bytes | None = None) -> EmailMessage:
    """`summary`: digest() sections, or plain lines (shown as one section). `chart`: a PNG shown in the email."""
    today = today or dt.date.today()
    msg = EmailMessage()
    msg["Subject"] = f"Trend Bot daily report, {today:%a %d %b %Y}"
    msg["From"], msg["To"] = cfg["EMAIL_USER"], cfg["EMAIL_TO"]
    sections = summary if summary and isinstance(summary[0], tuple) else \
        [("Summary", [plain(x) for x in summary if x])] if summary else []
    footer = ["The full report with charts is attached: open it in your browser." if report
              else "No report file was made tonight.",
              "Past patterns, not certainties; not financial advice."]
    from trend_bot import runs

    if runs.current_stamp():
        footer.append(f"Made by: {runs.current_stamp()}")
    msg.set_content(_text(sections) + "\n\n" + "\n".join(footer))
    msg.add_alternative(_html(sections, footer, chart_title if chart else None, demo_chart is not None), subtype="html")
    if chart:
        msg.get_payload()[1].add_related(chart, "image", "png", cid="<growth>")
    if demo_chart:   # the demo accounts vs the S&P 500, under the first section (the real record)
        msg.get_payload()[1].add_related(demo_chart, "image", "png", cid="<demo>")
    if report:
        msg.add_attachment(report.read_bytes(), maintype="text", subtype="html", filename=f"report-{today}.html")
    return msg


def send(cfg: dict, msg: EmailMessage, timeout: float = 30) -> None:
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ssl.create_default_context(), timeout=timeout) as s:
        s.login(cfg["EMAIL_USER"], cfg["EMAIL_APP_PASSWORD"])
        s.send_message(msg)
