import datetime as dt

from trend_bot import collect, db, mirror


class Resp:
    def __init__(self, body, code=200):
        self.body, self.status_code = body, code

    def json(self):
        return self.body

    def raise_for_status(self):
        assert self.status_code < 400


class NewsSession:
    def __init__(self):
        self.calls = []

    def get(self, url, params, timeout):
        self.calls.append(params)
        page = params.get("page_token")
        item = {"id": 2 if page else 1, "created_at": "2026-10-23T14:00:00Z", "symbols": ["AAPL"],
                "headline": "Strong record profit beats estimates" if not page else "Apple faces lawsuit and losses",
                "summary": "", "source": "benzinga", "url": "u"}
        return Resp({"news": [item], "next_page_token": None if page else "p2"})


def test_news_pages_are_followed_and_stored_once(tmp_path):
    con = db.connect(tmp_path / "m.db")
    s = NewsSession()
    assert collect.news(con, ["AAPL"], s, dt.date(2026, 10, 20), log=lambda *_: None, pause=0) == 2
    assert collect.news(con, ["AAPL"], s, dt.date(2026, 10, 20), log=lambda *_: None, pause=0) == 0   # once each
    tones = dict(con.execute("SELECT id, tone FROM news"))
    assert tones[1] > 0 > tones[2]
    assert s.calls[0]["symbols"] == "AAPL" and s.calls[0]["start"] == "2026-10-20T00:00:00Z"


class FredSession:
    def get(self, url, params, timeout):
        assert params["api_key"] == "k"
        return Resp({"observations": [{"date": "2026-10-21", "value": "4.1"}, {"date": "2026-10-22", "value": "."}]})


def test_macro_series_are_stored_and_continued(tmp_path):
    con = db.connect(tmp_path / "m.db")
    assert collect.macro(con, "k", FredSession(), log=lambda *_: None) == len(collect.SERIES)   # "." = missing
    assert con.execute("SELECT COUNT(*) FROM macro").fetchone()[0] == len(collect.SERIES)


def test_followed_stocks(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    con = db.connect(tmp_path / "m.db")
    con.execute("INSERT INTO ideas_holdings(ticker, rank) VALUES ('BRK-B', 1)")
    (tmp_path / "watchlist.txt").write_text("nvda\n# a comment\n")
    assert collect.followed(con) == ["BRK.B", "NVDA"]


class Cursor:
    def __init__(self, remote):
        self.remote, self.sql = remote, []

    def execute(self, sql, params=()):
        self.sql.append((sql, params))

    def fetchall(self):
        return self.remote


class Conn:
    def __init__(self, remote):
        self.c = Cursor(remote)

    def cursor(self):
        return self.c

    def commit(self):
        pass


def test_the_mirror_sends_only_whats_new_and_resends_a_readjusted_stock(tmp_path):
    con = db.connect(tmp_path / "m.db")
    rows = [(t, f"2026-10-{d:02d}", 1, 1, 1, c, c, 10) for t, c in (("AAA", 10.0), ("BBB", 20.0), ("CCC", 30.0))
            for d in (20, 21, 22)]
    con.executemany("INSERT INTO prices VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    con.execute("INSERT INTO tickers(ticker, name) VALUES ('AAA', 'A Corp')")
    con.commit()
    remote = [("AAA", "2026-10-21", 10.0),        # 1 day behind: send the 22nd
              ("BBB", "2026-10-22", 19.0)]        # its old price changed (a split): send it all again
    todo, full = mirror.plan(con, {t: (d, v) for t, d, v in remote})
    assert todo == {"AAA": "2026-10-21", "BBB": None, "CCC": None} and full == 2
    conn = Conn(remote)
    out = mirror.sync(conn, con, log=lambda *_: None)
    assert out == {"stocks": 3, "rows": 1 + 3 + 3, "tickers": 1}
    assert any(s.startswith("DELETE FROM dbo.prices WHERE ticker") and p == ("BBB",) for s, p in conn.c.sql)
    inserts = [p for s, p in conn.c.sql if s.startswith("INSERT INTO dbo.prices")]
    assert sum(len(p) // 8 for p in inserts) == 7


def test_big_inserts_are_split_into_1000_row_statements():
    c = Cursor([])
    mirror.insert(c, "prices", "a, b", [(i, i) for i in range(2500)])
    assert [len(p) // 2 for _, p in c.sql] == [1000, 1000, 500]
