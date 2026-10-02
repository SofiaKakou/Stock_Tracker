import json
from types import SimpleNamespace as NS

import numpy as np
import pandas as pd

from test_model import DATES, model_db  # noqa: F401
from trend_bot import ai_reader, text_changes


REPORT = ("<html><ix:header>hidden 2024</ix:header><body>"
          "<p>Table of contents</p><p>Item 7. Management's Discussion and Analysis 30</p>"
          "<p>Item 7A. Quantitative and Qualitative Disclosures About Market Risk 45</p>"
          "<p>Item 7. Management&#8217;s Discussion and Analysis of Financial Condition</p>"
          + "<p>Acme Widgets Inc. (ACME) grew revenue in 2024 compared with 2023, and Acme's margins rose.</p>" * 60
          + "<p>Item 7A. Quantitative and Qualitative Disclosures About Market Risk</p><p>rates</p></body></html>").encode()


def test_mdna_is_found_past_the_table_of_contents_and_anonymized():
    text = ai_reader.plain_text(REPORT)
    assert "hidden" not in text
    section = ai_reader.mdna(text)
    assert section.startswith("Management’s Discussion and Analysis of Financial Condition")
    assert "rates" not in section and "grew revenue" in section
    out = ai_reader.anonymize(section, ["Acme Widgets Inc."], ["ACME"], 2025)
    assert "Acme" not in out and "ACME" not in out and "2024" not in out
    assert "the Company (the Company) grew revenue in Y-1 compared with Y-2, and the Company margins rose." in out


def test_request_asks_for_structured_json_from_the_chosen_model():
    req = ai_reader.build_request("acc-1", "text")
    p = req["params"]
    assert req["custom_id"] == "acc-1" and p["model"] == "claude-opus-5-5"
    assert p["output_config"]["format"]["type"] == "json_schema"
    assert set(ai_reader.SCHEMA["required"]) == {"outlook", "confidence", "reasons", "recognized"}
    assert "thinking" not in p and "temperature" not in p


class FakeClient:
    """Stands in for anthropic.Anthropic(): no network, no cost."""

    def __init__(self, answers):
        self.answers, self.sent = answers, []
        self.messages = NS(batches=NS(create=self.create, retrieve=self.retrieve, results=self.results))

    def create(self, requests):
        self.sent = requests
        return NS(id="batch_1")

    def retrieve(self, batch_id):
        return NS(processing_status="ended")

    def results(self, batch_id):
        for r in self.sent:
            acc = r["custom_id"]
            if acc not in self.answers:
                yield NS(custom_id=acc, result=NS(type="errored"))
                continue
            msg = NS(content=[NS(type="thinking"), NS(type="text", text=json.dumps(self.answers[acc]))],
                     usage=NS(input_tokens=10_000, output_tokens=1_500))
            yield NS(custom_id=acc, result=NS(type="succeeded", message=msg))


def test_sample_send_collect_and_grade(model_db, tmp_path):  # noqa: F811
    con = model_db
    # Every company files one readable 10-K inside the window.
    start = DATES[300]
    rows = [(f"acc{i}", i, "10-K", str((start + pd.Timedelta(days=i)).date()), None, f"r{i}.htm")
            for i in range(1, 41)]
    con.executemany("INSERT INTO filings VALUES (?, ?, ?, ?, ?, ?)", rows)
    vec = text_changes.pack(np.ones(text_changes.BUCKETS))
    con.executemany("INSERT INTO doc_vectors(accession, words, vec) VALUES (?, 5000, ?)", [(r[0], vec) for r in rows])
    con.commit()
    picks = ai_reader.sample(con, 30, since=str(start.date()), until=str(DATES[-1].date()))
    assert len(picks) == 30 and picks["accession"].is_unique
    reqs = ai_reader.prepare(con, picks, fetch=lambda url: REPORT, log=lambda *_: None)
    assert len(reqs) == 30
    text = reqs[0]["params"]["messages"][0]["content"]
    assert "S0" not in text.split("report_excerpt")[1]   # tickers like S05 never reach the model

    # The fake model likes the stronger stocks (higher numbers) and recognizes one company.
    answers = {r["custom_id"]: {"outlook": int(r["custom_id"][3:]) // 8 - 2, "confidence": "medium",
                                "reasons": ["steady growth"], "recognized": "unknown"} for r in reqs[1:]}
    answers[reqs[2]["custom_id"]]["recognized"] = "Acme Widgets"
    client = FakeClient(answers)
    batch_id = ai_reader.submit(con, client, reqs)
    assert ai_reader.pending_batches(con) == [batch_id]
    assert ai_reader.collect(con, client, batch_id, log=lambda *_: None)
    assert ai_reader.pending_batches(con) == []
    assert con.execute("SELECT COUNT(*) FROM ai_scores").fetchone()[0] == 29   # one errored

    g = ai_reader.graded(con)
    assert g["excess"].notna().sum() > 20
    s = ai_reader.summary(con)
    assert "29 reports scored" in s and "$" in s and "Recognized the company despite the hiding: 1 of 29" in s
    assert "IC" in s
    # 29 x (10k in at $2/M + 1.5k out at $10/M) = $1.015
    assert "Cost: $1.01 in total" in s or "Cost: $1.02 in total" in s


def test_cli_refuses_without_key_and_summary_is_free(model_db, tmp_path, monkeypatch, capsys):  # noqa: F811
    from trend_bot.cli import main

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    path = str(tmp_path / "market.db")
    assert main(["ai", "sample", "--db", path]) == 1
    assert main(["ai", "summary", "--db", path]) == 0
    assert "No AI scores yet" in capsys.readouterr().out
