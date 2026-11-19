"""End-to-end and unit tests for the month-end close.

The synthetic generator plants known issues in the last month; the pipeline must find
every one of them, and nothing in the clean months.
"""
import sqlite3
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from close import load_config, run  # noqa: E402
from src import ingest, variance  # noqa: E402

CFG = load_config()
ISSUE_MONTH, CLEAN_MONTH = CFG["generate"]["last_month"], "2026-09"


@pytest.fixture(scope="session", autouse=True)
def data():
    if not (ROOT / CFG["paths"]["raw"] / ISSUE_MONTH).exists():
        subprocess.run([sys.executable, str(ROOT / "generate_data.py")], check=True, cwd=ROOT)


@pytest.fixture(scope="session")
def oct_close():
    return run(ISSUE_MONTH, report=False, quiet=True)


@pytest.fixture(scope="session")
def sep_close():
    return run(CLEAN_MONTH, report=False, quiet=True)


# ------------------------------------------------------------------ data quality
def test_detects_every_planted_data_quality_issue(oct_close):
    checks = set(oct_close["dq"].query("severity == 'warning'")["check"])
    assert checks == {
        "Inconsistent date format (ISO in dd/mm/yyyy column)",
        "Duplicate journal entry",
        "Posting date outside close period (cut-off)",
        "Missing cost center",
        "Duplicate transaction id",
        "Missing fee",
    }


def test_clean_month_has_no_warnings(sep_close):
    assert (sep_close["dq"]["severity"] == "info").all()


# ------------------------------------------------------------------ reconciliations
def test_all_reconciliations_fully_explained(oct_close):
    assert (oct_close["recs"]["residual"].abs() <= CFG["recon_tolerance_usd"]).all()
    assert not oct_close["blocking"]


def test_bank_reconciliation_items_are_the_planted_ones(oct_close):
    items = oct_close["rec_items"]
    bank_a = items[items["reconciliation"].str.startswith("Bank A")]
    assert sorted(bank_a["category"]) == sorted([
        "Deposit in transit", "Outstanding payment", "Unrecorded bank charges", "Amount difference (book error)"])


def test_root_causes(oct_close):
    items = oct_close["rec_items"].set_index("category")
    assert items.loc["Employee not accrued", "difference"] == pytest.approx(-(3800 * 1.215), abs=0.01)
    assert oct_close["rec_items"].query("ref == 'PAY-202610-003'")["difference"].iloc[0] == pytest.approx(-180.0)
    assert "Cut-off error" in items.index and "Reversals booked as revenue" in items.index


def test_clean_month_reconciles_with_no_open_items(sep_close):
    assert (sep_close["recs"]["status"] == "Reconciled").all()
    assert sep_close["rec_items"].empty


# ------------------------------------------------------------------ P&L
def test_pnl_ties_to_general_ledger(oct_close):
    gl = oct_close["gl_month"]
    revenue_gl = -gl[gl["account"].between(4000, 4999)]["amount"].sum()
    opex_gl = gl[gl["account"].between(6000, 6999)]["amount"].sum()
    s = oct_close["summary"]
    assert s.loc["Revenue", "actual_month"] == pytest.approx(revenue_gl, abs=0.01)
    assert s.loc["Operating expenses", "actual_month"] == pytest.approx(opex_gl, abs=0.01)


def test_commentary_explains_planted_overspends(oct_close):
    text = " ".join(c["comment"] for c in oct_close["commentary"])
    assert "Q4 brand campaign" in text and "Statutory audit" in text


def test_rerun_is_idempotent():
    run(ISSUE_MONTH, report=False, quiet=True)
    run(ISSUE_MONTH, report=False, quiet=True)
    con = sqlite3.connect(ROOT / CFG["paths"]["warehouse"])
    n = con.execute("SELECT COUNT(*) FROM fact_gl WHERE period = ?", (ISSUE_MONTH,)).fetchone()[0]
    con.close()
    assert n == 103


# ------------------------------------------------------------------ units
def test_european_number_parsing():
    assert ingest._eu_num(pd.Series(["-7.425,00", "326.373,88"])).tolist() == [-7425.0, 326373.88]


@pytest.mark.parametrize("actual,budget,is_rev,expected", [
    (62000, 25000, False, "Adverse"),      # cost overspend, both thresholds breached
    (25600, 25000, False, ""),             # small: below $5K
    (420000, 368000, True, "Favourable"),  # revenue beat: +$52K, +14%
    (400000, 368000, True, ""),            # +$32K but only +8.7%: not material
    (1000, 100, False, ""),                # +900%, but only $900
])
def test_materiality_flag(actual, budget, is_rev, expected):
    assert variance.flag(actual, budget, is_rev, 5000, 0.10)[2] == expected
