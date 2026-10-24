-- The star schema (schema dw) in TrendBotTrading: the journal's dbo tables are the staging area, and
-- dw.usp_Load turns them into dimensions (who/what/when) and facts (the numbers) every night.
-- Run as a whole by journal.build_dw (split on GO); safe to run again: tables are made only when
-- missing, the procedure and views are replaced. Every dimension has an "unknown" row with key -1.

IF SCHEMA_ID('dw') IS NULL EXEC('CREATE SCHEMA dw');
GO

-- === Dimensions =====================================================================================

IF OBJECT_ID('dw.DimDate', 'U') IS NULL
CREATE TABLE dw.DimDate (
    DateKey      INT NOT NULL PRIMARY KEY,          -- yyyymmdd, e.g. 20261023
    [Date]       DATE NULL,
    [Year]       SMALLINT NULL,
    [Quarter]    TINYINT NULL,
    [Month]      TINYINT NULL,
    MonthName    VARCHAR(10) NULL,
    YearMonth    CHAR(7) NULL,                      -- 2026-10
    [Day]        TINYINT NULL,
    DayOfWeek    TINYINT NULL,                      -- 1 = Monday ... 7 = Sunday
    DayName      VARCHAR(10) NULL,
    IsWeekend    BIT NULL
);
GO

IF OBJECT_ID('dw.DimStock', 'U') IS NULL
CREATE TABLE dw.DimStock (
    StockKey        INT IDENTITY(1, 1) NOT NULL PRIMARY KEY,
    Ticker          VARCHAR(12) NOT NULL UNIQUE,
    Name            NVARCHAR(300) NULL,
    Exchange        VARCHAR(40) NULL,
    Sector          NVARCHAR(100) NULL,
    CIK             INT NULL,
    IsFund          BIT NULL,                       -- fund, ETF or ETN
    FirstPriceDate  DATE NULL,
    LastPriceDate   DATE NULL,
    LastClose       FLOAT NULL,
    DollarVolume    FLOAT NULL,                     -- average $ traded a day (last 20 days)
    InPortfolio     BIT NOT NULL DEFAULT 0,         -- held by the monthly portfolio now
    UpdatedAt       DATETIME2(0) NULL
);
GO

IF OBJECT_ID('dw.DimPattern', 'U') IS NULL
CREATE TABLE dw.DimPattern (
    PatternKey   INT IDENTITY(1, 1) NOT NULL PRIMARY KEY,
    Pattern      VARCHAR(30) NOT NULL UNIQUE,       -- as the day-trading log names it (gap, breakout, ...)
    StudyName    VARCHAR(30) NULL,                  -- as the 10-year study names it (gap_fade, ...)
    Description  NVARCHAR(200) NULL,
    IsLive       BIT NOT NULL DEFAULT 0,            -- traded by the day-trading demo account
    StudyPerTrade FLOAT NULL,                       -- the latest 10-year test, after costs
    StudyYearly  FLOAT NULL,
    StudyVerdict NVARCHAR(200) NULL,
    StudyDate    DATE NULL
);
GO

IF OBJECT_ID('dw.DimAccount', 'U') IS NULL
CREATE TABLE dw.DimAccount (
    AccountKey   INT IDENTITY(1, 1) NOT NULL PRIMARY KEY,
    Account      VARCHAR(10) NOT NULL UNIQUE,
    Name         NVARCHAR(100) NULL
);
GO

IF OBJECT_ID('dw.DimMeasure', 'U') IS NULL
CREATE TABLE dw.DimMeasure (
    MeasureKey   INT IDENTITY(1, 1) NOT NULL PRIMARY KEY,
    Measure      VARCHAR(40) NOT NULL UNIQUE
);
GO

IF OBJECT_ID('dw.DimSeries', 'U') IS NULL
CREATE TABLE dw.DimSeries (
    SeriesKey    INT IDENTITY(1, 1) NOT NULL PRIMARY KEY,
    Series       VARCHAR(20) NOT NULL UNIQUE,       -- FRED code
    Description  NVARCHAR(200) NULL
);
GO

IF OBJECT_ID('dw.DimNews', 'U') IS NULL
CREATE TABLE dw.DimNews (
    NewsId       BIGINT NOT NULL PRIMARY KEY,
    Created      DATETIME2(0) NULL,
    Headline     NVARCHAR(1000) NULL,
    Summary      NVARCHAR(MAX) NULL,
    Source       NVARCHAR(100) NULL,
    Url          NVARCHAR(1000) NULL,
    Tone         FLOAT NULL                         -- (positive - negative words) / words
);
GO

-- === Facts ==========================================================================================

IF OBJECT_ID('dw.FactForecast', 'U') IS NULL
CREATE TABLE dw.FactForecast (                      -- the ranking: every ranked stock on each snapshot day
    DateKey      INT NOT NULL REFERENCES dw.DimDate (DateKey),
    StockKey     INT NOT NULL REFERENCES dw.DimStock (StockKey),
    [Rank]       INT NULL,                          -- 1 = best
    Score        FLOAT NULL,
    Pct          FLOAT NULL,                        -- 0-1, 1 = best
    Signals      INT NULL,                          -- how many measures it had
    PRIMARY KEY (DateKey, StockKey)
);
GO

IF OBJECT_ID('dw.FactForecastMeasure', 'U') IS NULL
CREATE TABLE dw.FactForecastMeasure (               -- each measure behind the ranking, per stock and day
    DateKey      INT NOT NULL REFERENCES dw.DimDate (DateKey),
    StockKey     INT NOT NULL REFERENCES dw.DimStock (StockKey),
    MeasureKey   INT NOT NULL REFERENCES dw.DimMeasure (MeasureKey),
    Pct          FLOAT NULL,                        -- 0-1, 1 = best
    PRIMARY KEY (DateKey, StockKey, MeasureKey)
);
GO

IF OBJECT_ID('dw.FactDayTrade', 'U') IS NULL
CREATE TABLE dw.FactDayTrade (
    DateKey      INT NOT NULL REFERENCES dw.DimDate (DateKey),
    StockKey     INT NOT NULL REFERENCES dw.DimStock (StockKey),
    PatternKey   INT NOT NULL REFERENCES dw.DimPattern (PatternKey),
    Side         SMALLINT NULL,                     -- 1 = bought, -1 = sold short
    Qty          FLOAT NULL,
    Signal       FLOAT NULL,
    Expected     FLOAT NULL,                        -- price when the bot decided
    Entry        FLOAT NULL,                        -- price it got
    ExitPrice    FLOAT NULL,
    Slippage     FLOAT NULL,
    Ret          FLOAT NULL,
    Pnl          FLOAT NULL,                        -- $ result
    EntryTime    VARCHAR(5) NULL,
    Bid          FLOAT NULL,
    Ask          FLOAT NULL,
    Spread       FLOAT NULL,
    PRIMARY KEY (DateKey, StockKey)
);
GO

IF OBJECT_ID('dw.FactDayPattern', 'U') IS NULL
CREATE TABLE dw.FactDayPattern (                    -- each day's result per pattern
    DateKey      INT NOT NULL REFERENCES dw.DimDate (DateKey),
    PatternKey   INT NOT NULL REFERENCES dw.DimPattern (PatternKey),
    Pnl          FLOAT NULL,
    Trades       INT NULL,
    PRIMARY KEY (DateKey, PatternKey)
);
GO

IF OBJECT_ID('dw.FactAccountValue', 'U') IS NULL
CREATE TABLE dw.FactAccountValue (
    DateKey      INT NOT NULL REFERENCES dw.DimDate (DateKey),
    AccountKey   INT NOT NULL REFERENCES dw.DimAccount (AccountKey),
    Value        FLOAT NULL,
    DayChange    FLOAT NULL,                        -- vs the day before, e.g. 0.01 = +1%
    PRIMARY KEY (DateKey, AccountKey)
);
GO

IF OBJECT_ID('dw.FactHolding', 'U') IS NULL
CREATE TABLE dw.FactHolding (                       -- what the monthly portfolio held from each update
    DateKey      INT NOT NULL REFERENCES dw.DimDate (DateKey),
    StockKey     INT NOT NULL REFERENCES dw.DimStock (StockKey),
    Bought       BIT NULL,                          -- new that month
    PRIMARY KEY (DateKey, StockKey)
);
GO

IF OBJECT_ID('dw.FactSafetyEvent', 'U') IS NULL
CREATE TABLE dw.FactSafetyEvent (
    DateKey      INT NOT NULL REFERENCES dw.DimDate (DateKey),
    StockKey     INT NOT NULL REFERENCES dw.DimStock (StockKey),
    Kind         VARCHAR(10) NOT NULL,              -- exit or warn
    Reason       NVARCHAR(400) NULL,
    PRIMARY KEY (DateKey, StockKey, Kind)
);
GO

IF OBJECT_ID('dw.FactNews', 'U') IS NULL
CREATE TABLE dw.FactNews (                          -- one row per headline and stock it mentions
    NewsId       BIGINT NOT NULL REFERENCES dw.DimNews (NewsId),
    DateKey      INT NOT NULL REFERENCES dw.DimDate (DateKey),
    StockKey     INT NOT NULL REFERENCES dw.DimStock (StockKey),
    Tone         FLOAT NULL,
    PRIMARY KEY (NewsId, StockKey)
);
GO

IF OBJECT_ID('dw.FactForecastOutcome', 'U') IS NULL
CREATE TABLE dw.FactForecastOutcome (               -- what each ranked stock did after the snapshot
    DateKey      INT NOT NULL REFERENCES dw.DimDate (DateKey),       -- the snapshot day
    StockKey     INT NOT NULL REFERENCES dw.DimStock (StockKey),
    Days         INT NULL,                          -- trading days measured (21 = the full month)
    Ret          FLOAT NULL,                        -- its return from the snapshot day's close
    MarketRet    FLOAT NULL,                        -- the S&P 500 (SPY) over the same days
    IsFinal      BIT NULL,                          -- the whole month has passed
    PRIMARY KEY (DateKey, StockKey)
);
GO

IF OBJECT_ID('dw.FactMacro', 'U') IS NULL
CREATE TABLE dw.FactMacro (
    DateKey      INT NOT NULL REFERENCES dw.DimDate (DateKey),
    SeriesKey    INT NOT NULL REFERENCES dw.DimSeries (SeriesKey),
    Value        FLOAT NULL,
    PRIMARY KEY (DateKey, SeriesKey)
);
GO

-- === The nightly load ===============================================================================

CREATE OR ALTER PROCEDURE dw.usp_Load
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;
    DECLARE @now DATETIME2(0) = SYSUTCDATETIME();

    -- Dates: 1990-2040 once, plus the unknown day.
    IF NOT EXISTS (SELECT 1 FROM dw.DimDate)
    BEGIN
        INSERT INTO dw.DimDate (DateKey) VALUES (-1);
        WITH n AS (SELECT TOP (DATEDIFF(DAY, '1990-01-01', '2040-12-31') + 1)
                          ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) - 1 AS i
                   FROM sys.all_objects a CROSS JOIN sys.all_objects b),
             d AS (SELECT DATEADD(DAY, i, CAST('1990-01-01' AS DATE)) AS dt FROM n)
        INSERT INTO dw.DimDate (DateKey, [Date], [Year], [Quarter], [Month], MonthName, YearMonth, [Day],
                                DayOfWeek, DayName, IsWeekend)
        SELECT CONVERT(INT, CONVERT(CHAR(8), dt, 112)), dt, YEAR(dt), DATEPART(QUARTER, dt), MONTH(dt),
               DATENAME(MONTH, dt), CONVERT(CHAR(7), dt, 126), DAY(dt),
               (DATEDIFF(DAY, '1900-01-01', dt) % 7) + 1, DATENAME(WEEKDAY, dt),
               CASE WHEN (DATEDIFF(DAY, '1900-01-01', dt) % 7) >= 5 THEN 1 ELSE 0 END
        FROM d;
    END;

    -- Unknown members.
    IF NOT EXISTS (SELECT 1 FROM dw.DimStock WHERE StockKey = -1)
    BEGIN
        SET IDENTITY_INSERT dw.DimStock ON;
        INSERT INTO dw.DimStock (StockKey, Ticker, Name) VALUES (-1, '?', N'Unknown');
        SET IDENTITY_INSERT dw.DimStock OFF;
    END;
    IF NOT EXISTS (SELECT 1 FROM dw.DimPattern WHERE PatternKey = -1)
    BEGIN
        SET IDENTITY_INSERT dw.DimPattern ON;
        INSERT INTO dw.DimPattern (PatternKey, Pattern, Description) VALUES (-1, '?', N'Unknown');
        SET IDENTITY_INSERT dw.DimPattern OFF;
    END;
    IF NOT EXISTS (SELECT 1 FROM dw.DimAccount WHERE AccountKey = -1)
    BEGIN
        SET IDENTITY_INSERT dw.DimAccount ON;
        INSERT INTO dw.DimAccount (AccountKey, Account, Name) VALUES (-1, '?', N'Unknown');
        SET IDENTITY_INSERT dw.DimAccount OFF;
    END;
    IF NOT EXISTS (SELECT 1 FROM dw.DimMeasure WHERE MeasureKey = -1)
    BEGIN
        SET IDENTITY_INSERT dw.DimMeasure ON;
        INSERT INTO dw.DimMeasure (MeasureKey, Measure) VALUES (-1, '?');
        SET IDENTITY_INSERT dw.DimMeasure OFF;
    END;
    IF NOT EXISTS (SELECT 1 FROM dw.DimSeries WHERE SeriesKey = -1)
    BEGIN
        SET IDENTITY_INSERT dw.DimSeries ON;
        INSERT INTO dw.DimSeries (SeriesKey, Series, Description) VALUES (-1, '?', N'Unknown');
        SET IDENTITY_INSERT dw.DimSeries OFF;
    END;

    -- Stocks: every listed stock (dbo.stocks), plus any the bot traded or ranked that isn't listed
    -- (tickers in the trading log use a dot, BRK.B; the price data a dash, BRK-B).
    MERGE dw.DimStock AS t
    USING (SELECT ticker, name, exchange, sector, cik, is_fund, first_date, last_date, last_close, dollar_vol,
                  in_portfolio FROM dbo.stocks) AS s
    ON t.Ticker = s.ticker
    WHEN MATCHED THEN UPDATE SET Name = s.name, Exchange = s.exchange, Sector = COALESCE(s.sector, t.Sector),
        CIK = s.cik, IsFund = s.is_fund, FirstPriceDate = s.first_date, LastPriceDate = s.last_date,
        LastClose = s.last_close, DollarVolume = s.dollar_vol, InPortfolio = s.in_portfolio, UpdatedAt = @now
    WHEN NOT MATCHED BY TARGET THEN INSERT (Ticker, Name, Exchange, Sector, CIK, IsFund, FirstPriceDate,
        LastPriceDate, LastClose, DollarVolume, InPortfolio, UpdatedAt)
        VALUES (s.ticker, s.name, s.exchange, s.sector, s.cik, s.is_fund, s.first_date, s.last_date,
                s.last_close, s.dollar_vol, s.in_portfolio, @now);

    UPDATE dw.DimStock SET InPortfolio = 0, UpdatedAt = @now
    WHERE InPortfolio = 1 AND StockKey <> -1 AND Ticker NOT IN (SELECT ticker FROM dbo.stocks WHERE in_portfolio = 1);

    INSERT INTO dw.DimStock (Ticker, UpdatedAt)
    SELECT DISTINCT x.ticker, @now
    FROM (SELECT REPLACE(symbol, '.', '-') AS ticker FROM dbo.day_trades
          UNION SELECT ticker FROM dbo.monthly_holdings
          UNION SELECT ticker FROM dbo.ranking_snapshots
          UNION SELECT ticker FROM dbo.safety_events
          UNION SELECT ticker FROM dbo.forecast_outcomes
          UNION SELECT REPLACE(LTRIM(RTRIM(v.value)), '.', '-') FROM dbo.news n
                CROSS APPLY STRING_SPLIT(n.symbols, ',') v) x
    WHERE x.ticker <> '' AND NOT EXISTS (SELECT 1 FROM dw.DimStock d WHERE d.Ticker = x.ticker);

    -- Sector from the ranking where the SEC code hasn't given one.
    UPDATE d SET Sector = r.sector
    FROM dw.DimStock d
    JOIN (SELECT ticker, sector, ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY snapshot_date DESC) AS n
          FROM dbo.ranking_snapshots WHERE sector IS NOT NULL) r ON r.ticker = d.Ticker AND r.n = 1
    WHERE d.Sector IS NULL;

    -- Patterns (the list comes from the bot's code; the latest study from dbo.study_results).
    MERGE dw.DimPattern AS t
    USING (VALUES /*PATTERNS*/) AS s (Pattern, StudyName, Description, IsLive)
    ON t.Pattern = s.Pattern
    WHEN MATCHED THEN UPDATE SET StudyName = s.StudyName, Description = s.Description, IsLive = s.IsLive
    WHEN NOT MATCHED BY TARGET THEN INSERT (Pattern, StudyName, Description, IsLive)
        VALUES (s.Pattern, s.StudyName, s.Description, s.IsLive);

    INSERT INTO dw.DimPattern (Pattern)
    SELECT DISTINCT x.pattern FROM (SELECT pattern FROM dbo.day_trades UNION SELECT pattern FROM dbo.day_summary) x
    WHERE x.pattern IS NOT NULL AND NOT EXISTS (SELECT 1 FROM dw.DimPattern p WHERE p.Pattern = x.pattern);

    UPDATE p SET StudyPerTrade = r.per_trade, StudyYearly = r.yearly, StudyVerdict = r.verdict, StudyDate = r.updated
    FROM dw.DimPattern p JOIN dbo.study_results r ON r.pattern = p.StudyName;

    -- Accounts, measures, economic series.
    MERGE dw.DimAccount AS t
    USING (VALUES /*ACCOUNTS*/) AS s (Account, Name)
    ON t.Account = s.Account
    WHEN MATCHED THEN UPDATE SET Name = s.Name
    WHEN NOT MATCHED BY TARGET THEN INSERT (Account, Name) VALUES (s.Account, s.Name);

    INSERT INTO dw.DimAccount (Account)
    SELECT DISTINCT account FROM dbo.account_values a
    WHERE NOT EXISTS (SELECT 1 FROM dw.DimAccount d WHERE d.Account = a.account);

    INSERT INTO dw.DimMeasure (Measure)
    SELECT DISTINCT measure FROM dbo.measure_snapshots m
    WHERE NOT EXISTS (SELECT 1 FROM dw.DimMeasure d WHERE d.Measure = m.measure);

    MERGE dw.DimSeries AS t
    USING (VALUES /*SERIES*/) AS s (Series, Description)
    ON t.Series = s.Series
    WHEN MATCHED THEN UPDATE SET Description = s.Description
    WHEN NOT MATCHED BY TARGET THEN INSERT (Series, Description) VALUES (s.Series, s.Description);

    INSERT INTO dw.DimSeries (Series)
    SELECT DISTINCT series FROM dbo.macro m
    WHERE NOT EXISTS (SELECT 1 FROM dw.DimSeries d WHERE d.Series = m.series);

    -- Facts. Small ones are rebuilt in full; the ranking, its measures and the news only add new days.
    BEGIN TRANSACTION;

    DELETE FROM dw.FactDayTrade;
    INSERT INTO dw.FactDayTrade (DateKey, StockKey, PatternKey, Side, Qty, Signal, Expected, Entry, ExitPrice,
                                 Slippage, Ret, Pnl, EntryTime, Bid, Ask, Spread)
    SELECT CONVERT(INT, CONVERT(CHAR(8), t.trade_date, 112)), ISNULL(s.StockKey, -1), ISNULL(p.PatternKey, -1),
           t.side, t.qty, t.signal, t.expected, t.entry, t.exit_price, t.slippage, t.ret, t.pnl, t.entry_time,
           t.bid, t.ask, t.spread
    FROM dbo.day_trades t
    LEFT JOIN dw.DimStock s ON s.Ticker = REPLACE(t.symbol, '.', '-')
    LEFT JOIN dw.DimPattern p ON p.Pattern = t.pattern;

    DELETE FROM dw.FactDayPattern;
    INSERT INTO dw.FactDayPattern (DateKey, PatternKey, Pnl, Trades)
    SELECT CONVERT(INT, CONVERT(CHAR(8), d.trade_date, 112)), ISNULL(p.PatternKey, -1), d.pnl, d.trades
    FROM dbo.day_summary d LEFT JOIN dw.DimPattern p ON p.Pattern = d.pattern;

    DELETE FROM dw.FactAccountValue;
    INSERT INTO dw.FactAccountValue (DateKey, AccountKey, Value, DayChange)
    SELECT CONVERT(INT, CONVERT(CHAR(8), v.value_date, 112)), ISNULL(a.AccountKey, -1), v.value,
           v.value / NULLIF(LAG(v.value) OVER (PARTITION BY v.account ORDER BY v.value_date), 0) - 1
    FROM dbo.account_values v LEFT JOIN dw.DimAccount a ON a.Account = v.account;

    DELETE FROM dw.FactHolding;
    INSERT INTO dw.FactHolding (DateKey, StockKey, Bought)
    SELECT CONVERT(INT, CONVERT(CHAR(8), h.portfolio_date, 112)), ISNULL(s.StockKey, -1), h.bought
    FROM dbo.monthly_holdings h LEFT JOIN dw.DimStock s ON s.Ticker = h.ticker;

    DELETE FROM dw.FactSafetyEvent;
    INSERT INTO dw.FactSafetyEvent (DateKey, StockKey, Kind, Reason)
    SELECT CONVERT(INT, CONVERT(CHAR(8), e.event_date, 112)), ISNULL(s.StockKey, -1), e.kind, e.reason
    FROM dbo.safety_events e LEFT JOIN dw.DimStock s ON s.Ticker = e.ticker;

    DELETE FROM dw.FactMacro;
    INSERT INTO dw.FactMacro (DateKey, SeriesKey, Value)
    SELECT CONVERT(INT, CONVERT(CHAR(8), m.value_date, 112)), ISNULL(s.SeriesKey, -1), m.value
    FROM dbo.macro m LEFT JOIN dw.DimSeries s ON s.Series = m.series;

    INSERT INTO dw.FactForecast (DateKey, StockKey, [Rank], Score, Pct, Signals)
    SELECT k.DateKey, ISNULL(s.StockKey, -1), r.[rank], r.score, r.pct, r.signals
    FROM dbo.ranking_snapshots r
    CROSS APPLY (SELECT CONVERT(INT, CONVERT(CHAR(8), r.snapshot_date, 112)) AS DateKey) k
    LEFT JOIN dw.DimStock s ON s.Ticker = r.ticker
    WHERE NOT EXISTS (SELECT 1 FROM dw.FactForecast f WHERE f.DateKey = k.DateKey);

    INSERT INTO dw.FactForecastMeasure (DateKey, StockKey, MeasureKey, Pct)
    SELECT k.DateKey, ISNULL(s.StockKey, -1), ISNULL(d.MeasureKey, -1), m.pct
    FROM dbo.measure_snapshots m
    CROSS APPLY (SELECT CONVERT(INT, CONVERT(CHAR(8), m.snapshot_date, 112)) AS DateKey) k
    LEFT JOIN dw.DimStock s ON s.Ticker = m.ticker
    LEFT JOIN dw.DimMeasure d ON d.Measure = m.measure
    WHERE NOT EXISTS (SELECT 1 FROM dw.FactForecastMeasure f WHERE f.DateKey = k.DateKey);

    INSERT INTO dw.DimNews (NewsId, Created, Headline, Summary, Source, Url, Tone)
    SELECT n.id, TRY_CONVERT(DATETIME2(0), LEFT(n.created, 19)), n.headline, n.summary, n.source, n.url, n.tone
    FROM dbo.news n WHERE NOT EXISTS (SELECT 1 FROM dw.DimNews d WHERE d.NewsId = n.id);

    INSERT INTO dw.FactNews (NewsId, DateKey, StockKey, Tone)
    SELECT DISTINCT n.id, ISNULL(CONVERT(INT, CONVERT(CHAR(8), n.news_date, 112)), -1), ISNULL(s.StockKey, -1), n.tone
    FROM dbo.news n
    CROSS APPLY STRING_SPLIT(n.symbols, ',') v
    LEFT JOIN dw.DimStock s ON s.Ticker = REPLACE(LTRIM(RTRIM(v.value)), '.', '-')
    WHERE LTRIM(RTRIM(v.value)) <> '' AND NOT EXISTS (SELECT 1 FROM dw.FactNews f WHERE f.NewsId = n.id);

    DELETE FROM dw.FactForecastOutcome;
    INSERT INTO dw.FactForecastOutcome (DateKey, StockKey, Days, Ret, MarketRet, IsFinal)
    SELECT CONVERT(INT, CONVERT(CHAR(8), o.snapshot_date, 112)), ISNULL(s.StockKey, -1), o.days, o.ret,
           o.market_ret, o.final
    FROM dbo.forecast_outcomes o LEFT JOIN dw.DimStock s ON s.Ticker = o.ticker;

    COMMIT TRANSACTION;

    SELECT 'DimStock' AS [Table], COUNT(*) AS [Rows] FROM dw.DimStock
    UNION ALL SELECT 'FactForecast', COUNT(*) FROM dw.FactForecast
    UNION ALL SELECT 'FactForecastMeasure', COUNT(*) FROM dw.FactForecastMeasure
    UNION ALL SELECT 'FactDayTrade', COUNT(*) FROM dw.FactDayTrade
    UNION ALL SELECT 'FactDayPattern', COUNT(*) FROM dw.FactDayPattern
    UNION ALL SELECT 'FactAccountValue', COUNT(*) FROM dw.FactAccountValue
    UNION ALL SELECT 'FactHolding', COUNT(*) FROM dw.FactHolding
    UNION ALL SELECT 'FactSafetyEvent', COUNT(*) FROM dw.FactSafetyEvent
    UNION ALL SELECT 'FactNews', COUNT(*) FROM dw.FactNews
    UNION ALL SELECT 'FactMacro', COUNT(*) FROM dw.FactMacro
    UNION ALL SELECT 'FactForecastOutcome', COUNT(*) FROM dw.FactForecastOutcome;
END;
GO

-- === Views for searching ============================================================================

-- One row per stock: what it is, and what the bot thinks of it now.
-- e.g. SELECT * FROM dw.vStockSearch WHERE Name LIKE '%apple%' OR Ticker = 'AAPL'
CREATE OR ALTER VIEW dw.vStockSearch AS
WITH latest AS (SELECT MAX(DateKey) AS DateKey FROM dw.FactForecast),
     news AS (SELECT f.StockKey, COUNT(*) AS Headlines30d, AVG(f.Tone) AS Tone30d
              FROM dw.FactNews f JOIN dw.DimDate d ON d.DateKey = f.DateKey
              WHERE d.[Date] >= DATEADD(DAY, -30, CAST(SYSUTCDATETIME() AS DATE))
              GROUP BY f.StockKey),
     traded AS (SELECT StockKey, COUNT(*) AS DayTrades, SUM(Pnl) AS DayTradePnl FROM dw.FactDayTrade GROUP BY StockKey)
SELECT s.StockKey, s.Ticker, s.Name, s.Sector, s.Exchange, s.IsFund, s.LastClose, s.LastPriceDate,
       s.DollarVolume, s.InPortfolio,
       f.[Rank] AS ForecastRank, f.Score AS ForecastScore, f.Pct AS ForecastPct, d.[Date] AS ForecastDate,
       n.Headlines30d, n.Tone30d, t.DayTrades, t.DayTradePnl
FROM dw.DimStock s
CROSS JOIN latest l
LEFT JOIN dw.FactForecast f ON f.StockKey = s.StockKey AND f.DateKey = l.DateKey
LEFT JOIN dw.DimDate d ON d.DateKey = f.DateKey
LEFT JOIN news n ON n.StockKey = s.StockKey
LEFT JOIN traded t ON t.StockKey = s.StockKey
WHERE s.StockKey <> -1;
GO

-- Every ranking, readable: SELECT * FROM dw.vForecast WHERE Ticker = 'AAPL' ORDER BY [Date]
CREATE OR ALTER VIEW dw.vForecast AS
SELECT d.[Date], s.Ticker, s.Name, s.Sector, f.[Rank], f.Score, f.Pct, f.Signals
FROM dw.FactForecast f
JOIN dw.DimDate d ON d.DateKey = f.DateKey
JOIN dw.DimStock s ON s.StockKey = f.StockKey;
GO

-- Every day trade, readable: SELECT * FROM dw.vDayTrades ORDER BY [Date] DESC
CREATE OR ALTER VIEW dw.vDayTrades AS
SELECT d.[Date], d.DayName, t.EntryTime, s.Ticker, s.Name, p.Pattern, p.Description AS PatternName,
       CASE t.Side WHEN 1 THEN 'long' WHEN -1 THEN 'short' END AS Side, t.Qty, t.Expected, t.Entry, t.ExitPrice,
       t.Slippage, t.Spread, t.Ret, t.Pnl
FROM dw.FactDayTrade t
JOIN dw.DimDate d ON d.DateKey = t.DateKey
JOIN dw.DimStock s ON s.StockKey = t.StockKey
JOIN dw.DimPattern p ON p.PatternKey = t.PatternKey;
GO

-- Did the forecast work? One row per snapshot: the bot's top 20 vs every other ranked stock vs the
-- S&P 500 over the next month (so far, until IsFinal). Over many snapshots, Top20MinusRest > 0 means
-- the ranking picks winners. e.g.
--   SELECT AVG(Top20MinusRest), AVG(Top20MinusMarket), COUNT(*) FROM dw.vForecastCheck WHERE IsFinal = 1
CREATE OR ALTER VIEW dw.vForecastCheck AS
SELECT d.[Date], MIN(o.Days) AS Days, CAST(MIN(CAST(o.IsFinal AS INT)) AS BIT) AS IsFinal,
       COUNT(*) AS Stocks,
       AVG(CASE WHEN f.[Rank] <= 20 THEN o.Ret END) AS Top20Return,
       AVG(CASE WHEN f.[Rank] > 20 THEN o.Ret END) AS RestReturn,
       AVG(o.MarketRet) AS MarketReturn,
       AVG(CASE WHEN f.[Rank] <= 20 THEN o.Ret END) - AVG(CASE WHEN f.[Rank] > 20 THEN o.Ret END) AS Top20MinusRest,
       AVG(CASE WHEN f.[Rank] <= 20 THEN o.Ret END) - AVG(o.MarketRet) AS Top20MinusMarket,
       AVG(CASE WHEN f.[Rank] <= 20 THEN CASE WHEN o.Ret > o.MarketRet THEN 1.0 ELSE 0 END END) AS Top20BeatMarketShare
FROM dw.FactForecastOutcome o
JOIN dw.FactForecast f ON f.DateKey = o.DateKey AND f.StockKey = o.StockKey
JOIN dw.DimDate d ON d.DateKey = o.DateKey
GROUP BY d.[Date];
GO

-- The same by tenth of the ranking (1 = the best 10%): the returns should fall from 1 to 10.
CREATE OR ALTER VIEW dw.vForecastDeciles AS
SELECT d.[Date], CASE WHEN f.Pct >= 0.9 THEN 1 WHEN f.Pct <= 0.1 THEN 10 ELSE CAST(FLOOR((1 - f.Pct) * 10) AS INT) + 1 END AS Decile, COUNT(*) AS Stocks,
       AVG(o.Ret) AS AvgReturn, AVG(o.Ret - o.MarketRet) AS VsMarket, CAST(MIN(CAST(o.IsFinal AS INT)) AS BIT) AS IsFinal
FROM dw.FactForecastOutcome o
JOIN dw.FactForecast f ON f.DateKey = o.DateKey AND f.StockKey = o.StockKey
JOIN dw.DimDate d ON d.DateKey = o.DateKey
WHERE f.Pct IS NOT NULL
GROUP BY d.[Date], CASE WHEN f.Pct >= 0.9 THEN 1 WHEN f.Pct <= 0.1 THEN 10 ELSE CAST(FLOOR((1 - f.Pct) * 10) AS INT) + 1 END;
GO

-- Each day-trading pattern: what the 10-year test promised vs what live trading delivers.
-- (Live returns are what the account got, fill to fill, so they include real costs like the
-- 10-year test's "after costs" number.) Judge only after ~20 live trades; a single day is luck.
CREATE OR ALTER VIEW dw.vPatternScorecard AS
WITH live AS (
    SELECT t.PatternKey, COUNT(*) AS Trades, COUNT(t.Ret) AS ClosedTrades, AVG(t.Ret) AS LivePerTrade,
           SUM(t.Pnl) AS LiveDollars, AVG(CASE WHEN t.Ret > 0 THEN 1.0 WHEN t.Ret IS NOT NULL THEN 0 END) AS HitRate,
           AVG(t.Slippage) AS AvgSlippage, AVG(t.Spread) AS AvgSpread,
           MIN(d.[Date]) AS FirstTrade, MAX(d.[Date]) AS LastTrade, COUNT(DISTINCT t.DateKey) AS Days
    FROM dw.FactDayTrade t JOIN dw.DimDate d ON d.DateKey = t.DateKey
    GROUP BY t.PatternKey)
SELECT p.Pattern, p.Description, p.IsLive, p.StudyPerTrade AS TestPerTrade, p.StudyYearly AS TestYearly,
       p.StudyVerdict AS TestVerdict, ISNULL(l.Trades, 0) AS Trades, ISNULL(l.ClosedTrades, 0) AS ClosedTrades,
       l.Days, l.LivePerTrade, l.LivePerTrade - p.StudyPerTrade AS LiveMinusTest, l.LiveDollars, l.HitRate,
       l.AvgSlippage, l.AvgSpread, l.FirstTrade, l.LastTrade,
       CASE WHEN ISNULL(l.ClosedTrades, 0) = 0 THEN 'no live trades yet'
            WHEN l.ClosedTrades < 20 THEN 'too early to tell'
            WHEN l.LivePerTrade > p.StudyPerTrade THEN 'live better than the test so far'
            ELSE 'live worse than the test so far' END AS Verdict
FROM dw.DimPattern p
LEFT JOIN live l ON l.PatternKey = p.PatternKey
WHERE p.PatternKey <> -1;
GO
