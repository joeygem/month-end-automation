"""SQLite warehouse: idempotent monthly loads and the P&L mart (in SQL)."""
import sqlite3
from pathlib import Path

import pandas as pd

SCHEMA = """
CREATE TABLE IF NOT EXISTS dim_account (account INTEGER PRIMARY KEY, name TEXT, type TEXT, line TEXT);
CREATE TABLE IF NOT EXISTS fact_gl (period TEXT, je_no TEXT, date TEXT, account INTEGER, cost_center TEXT,
    description TEXT, ref TEXT, debit REAL, credit REAL, amount REAL, orig_ccy TEXT, orig_amount REAL, source TEXT);
CREATE TABLE IF NOT EXISTS fact_budget (period TEXT, account INTEGER, cost_center TEXT, budget REAL);
CREATE TABLE IF NOT EXISTS fact_platform_daily (period TEXT, date TEXT, service TEXT, status TEXT,
    transactions INTEGER, volume_usd REAL, fees_usd REAL);
CREATE TABLE IF NOT EXISTS dq_issues (period TEXT, source TEXT, "check" TEXT, severity TEXT, rows INTEGER, action TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS recon_items (period TEXT, reconciliation TEXT, ref TEXT, category TEXT, date TEXT,
    gl_amount REAL, source_amount REAL, difference REAL, explanation TEXT);
CREATE TABLE IF NOT EXISTS run_log (run_at TEXT, period TEXT, step TEXT, seconds REAL, rows INTEGER, status TEXT);
CREATE INDEX IF NOT EXISTS ix_gl_period ON fact_gl(period, account);
"""

PNL_SQL = """
WITH actual AS (
    SELECT period, account, cost_center, SUM(amount) AS amount
    FROM fact_gl
    WHERE account >= 4000
    GROUP BY period, account, cost_center
),
keys AS (
    SELECT period, account, cost_center FROM actual
    UNION
    SELECT period, account, cost_center FROM fact_budget WHERE period <= :period
),
joined AS (
    SELECT k.period, k.account, k.cost_center,
           -- revenue is credit-normal: flip sign so revenue and costs are both positive
           CASE WHEN a.type = 'revenue' THEN -COALESCE(ac.amount, 0) ELSE COALESCE(ac.amount, 0) END AS actual,
           COALESCE(b.budget, 0) AS budget
    FROM keys k
    JOIN dim_account a USING (account)
    LEFT JOIN actual ac ON ac.period = k.period AND ac.account = k.account AND ac.cost_center = k.cost_center
    LEFT JOIN fact_budget b ON b.period = k.period AND b.account = k.account AND b.cost_center = k.cost_center
    WHERE k.period LIKE substr(:period, 1, 4) || '%' AND k.period <= :period
)
SELECT j.account, a.name, a.type, a.line, j.cost_center,
       SUM(CASE WHEN j.period = :period THEN j.actual ELSE 0 END) AS actual_month,
       SUM(CASE WHEN j.period = :period THEN j.budget ELSE 0 END) AS budget_month,
       SUM(j.actual) AS actual_ytd,
       SUM(j.budget) AS budget_ytd
FROM joined j JOIN dim_account a USING (account)
GROUP BY j.account, a.name, a.type, a.line, j.cost_center
ORDER BY j.account, j.cost_center;
"""

TREND_SQL = """
SELECT g.period,
       SUM(CASE WHEN a.type = 'revenue' THEN -g.amount ELSE 0 END) AS revenue,
       SUM(CASE WHEN a.line = 'Cost of revenue' THEN g.amount ELSE 0 END) AS cost_of_revenue,
       SUM(CASE WHEN a.line = 'Operating expenses' THEN g.amount ELSE 0 END) AS opex
FROM fact_gl g JOIN dim_account a USING (account)
WHERE g.period LIKE substr(:period, 1, 4) || '%' AND g.period <= :period
GROUP BY g.period ORDER BY g.period;
"""


class Warehouse:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.executescript(SCHEMA)

    def load_accounts(self, accounts: dict):
        rows = [(int(a), v["name"], v["type"], v.get("line")) for a, v in accounts.items()]
        self.conn.execute("DELETE FROM dim_account")
        self.conn.executemany("INSERT INTO dim_account VALUES (?,?,?,?)", rows)

    def replace_period(self, table: str, period: str, df: pd.DataFrame):
        """Idempotent load: re-running a close replaces that period instead of duplicating it."""
        self.conn.execute(f"DELETE FROM {table} WHERE period = ?", (period,))
        if len(df):
            df.assign(period=period).to_sql(table, self.conn, if_exists="append", index=False)

    def replace_budget(self, budget: pd.DataFrame):
        self.conn.execute("DELETE FROM fact_budget")
        budget.to_sql("fact_budget", self.conn, if_exists="append", index=False)

    def log(self, run_at, period, step, seconds, rows, status="ok"):
        self.conn.execute("INSERT INTO run_log VALUES (?,?,?,?,?,?)", (run_at, period, step, round(seconds, 3), rows, status))

    def query(self, sql: str, **params) -> pd.DataFrame:
        return pd.read_sql_query(sql, self.conn, params=params)

    def pnl(self, period: str) -> pd.DataFrame:
        return self.query(PNL_SQL, period=period)

    def trend(self, period: str) -> pd.DataFrame:
        return self.query(TREND_SQL, period=period)

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.commit()
        self.conn.close()
