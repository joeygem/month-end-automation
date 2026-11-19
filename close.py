"""Run the month-end close for one period.

    python close.py --period 2026-10
    python close.py --period 2026-10 --no-report   # checks and reconciliations only

Exit code 0 = closed cleanly, 1 = blocking issue (data-quality error or unexplained reconciliation difference).
"""
import argparse
import json
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml

from src import ingest, quality, reconcile, variance
from src.warehouse import Warehouse

ROOT = Path(__file__).resolve().parent


def load_config(path=ROOT / "config.yaml") -> dict:
    cfg = yaml.safe_load(Path(path).read_text())
    cfg["accounts"] = {int(k): v for k, v in cfg["accounts"].items()}
    cfg["bank_accounts"] = {int(k): v for k, v in cfg["bank_accounts"].items()}
    return cfg


def run(period: str, cfg: dict | None = None, raw: Path | None = None, report: bool = True, quiet: bool = False) -> dict:
    cfg = cfg or load_config()
    raw = raw or ROOT / cfg["paths"]["raw"]
    out_dir = ROOT / cfg["paths"]["output"] / period
    tol = cfg["recon_tolerance_usd"]
    run_at = datetime.now().isoformat(timespec="seconds")
    wh = Warehouse(ROOT / cfg["paths"]["warehouse"])
    wh.load_accounts(cfg["accounts"])
    log, t_start = [], time.perf_counter()

    def say(msg):
        if not quiet:
            print(msg)

    @contextmanager
    def step(name):
        t0 = time.perf_counter()
        info = {"rows": 0}
        yield info
        dt = time.perf_counter() - t0
        log.append({"step": name, "seconds": round(dt, 3), "rows": info["rows"]})
        wh.log(run_at, period, name, dt, info["rows"])
        say(f"  ✓ {name:<38} {info['rows']:>8,} rows  {dt:6.2f}s")

    say(f"\n{cfg['company']} · month-end close {period}")
    issues = []

    # 1. reference data
    with step("Load budget & FX rates") as s:
        budget = ingest.read_budget(raw / "reference" / f"budget_FY{cfg['fiscal_year']}.xlsx", cfg["fiscal_year"])
        fx = ingest.read_fx(raw / "reference" / f"fx_rates_{cfg['fiscal_year']}.csv")
        wh.replace_budget(budget)
        s["rows"] = len(budget)

    # 2. general ledger, year to date (idempotent per period)
    with step("Ingest & validate GL (YTD)") as s:
        months = pd.period_range(f"{cfg['fiscal_year']}-01", period, freq="M").astype(str)
        frames = []
        for m in months:
            g, iss = quality.clean_gl(ingest.read_gl(ingest.month_files(raw, m)["gl"]), m, set(cfg["accounts"]))
            g["period"] = m
            frames.append(g)
            if m == period:
                issues += iss
            elif any(i.severity == "error" for i in iss):
                raise RuntimeError(f"Prior period {m} has data-quality errors; re-close it first")
            cols = ["je_no", "date", "account", "cost_center", "description", "ref", "debit", "credit", "amount", "orig_ccy", "orig_amount", "source"]
            wh.replace_period("fact_gl", m, g[cols].assign(date=g["date"].dt.strftime("%Y-%m-%d")))
        gl = pd.concat(frames, ignore_index=True)
        gl_month = gl[gl["period"] == period]
        s["rows"] = len(gl)

    files = ingest.month_files(raw, period)
    rates = fx[fx["month"] == period].set_index("currency")["rate_to_usd"].to_dict()

    # 3. platform transactions
    with step("Ingest & validate platform transactions") as s:
        tx, iss = quality.clean_transactions(ingest.read_transactions(files["transactions"]), cfg["tariff"], rates)
        issues += iss
        daily = (tx.assign(date=tx["ts"].dt.strftime("%Y-%m-%d"))
                   .groupby(["date", "service", "status"], as_index=False)
                   .agg(transactions=("txn_id", "size"), volume_usd=("amount_usd", "sum"), fees_usd=("fee_usd", "sum")))
        wh.replace_period("fact_platform_daily", period, daily)
        s["rows"] = len(tx)

    # 4. bank statements + payroll
    with step("Ingest bank statements & payroll") as s:
        bank_a, opening_a = ingest.read_bank_a(files["bank_a"])
        issues += quality.check_bank_balance(bank_a, opening_a, files["bank_a"].name)
        bank_b, closing_b = ingest.read_bank_b(files["bank_b"])
        payroll = ingest.read_payroll(files["payroll"], cfg["departments"])
        s["rows"] = len(bank_a) + len(bank_b) + len(payroll)

    # 5. reconciliations
    with step("Run reconciliations") as s:
        recs = [
            reconcile.bank(gl, bank_a, float(bank_a["balance"].iloc[-1]), 1000, period, "Bank A (USD) vs GL 1000", tol),
            reconcile.bank(gl, bank_b, closing_b, 1010, period, "Bank B (EUR) vs GL 1010", tol, use_orig=True),
            reconcile.revenue(tx, gl, cfg["services"], period, tol),
            reconcile.payroll(payroll, gl, period, tol),
        ]
        rec_summary = pd.DataFrame([r[0] for r in recs])
        non_empty = [r[1] for r in recs if len(r[1])]
        rec_items = pd.concat(non_empty, ignore_index=True) if non_empty else recs[0][1]
        wh.replace_period("recon_items", period, rec_items.assign(date=rec_items["date"].astype(str)))
        s["rows"] = len(rec_items)

    # 6. P&L mart (SQL) + variance analysis
    with step("Build P&L & budget variance") as s:
        mart = wh.pnl(period)
        acc = variance.pnl_by_account(mart, cfg["materiality"]["abs_usd"], cfg["materiality"]["pct"])
        summary = variance.summary_lines(acc)
        trend = wh.trend(period)
        notes = variance.commentary(acc, mart[["account", "cost_center", "actual_month", "budget_month"]], gl_month, period)
        kpi = variance.kpis(tx, summary, trend)
        s["rows"] = len(mart)

    dq = quality.issues_frame(issues)
    wh.replace_period("dq_issues", period, dq)
    wh.commit()

    blocking = bool((dq["severity"] == "error").any() or (rec_summary["status"] == "Unexplained difference").any())
    result = {"period": period, "company": cfg["company"], "run_at": run_at, "kpis": kpi, "summary": summary, "accounts": acc,
              "mart": mart, "trend": trend, "commentary": notes, "recs": rec_summary, "rec_items": rec_items, "dq": dq,
              "gl_month": gl_month, "log": log, "blocking": blocking}

    # 7. outputs
    if report:
        from src import report_excel, report_html
        out_dir.mkdir(parents=True, exist_ok=True)
        with step("Write Excel management pack") as s:
            result["excel"] = report_excel.write(result, out_dir / f"{cfg['company'].replace(' ', '_')}_Close_Pack_{period}.xlsx")
            s["rows"] = len(acc)
        with step("Write HTML close summary") as s:
            result["html"] = report_html.write(result, out_dir / f"close_summary_{period}.html")
            s["rows"] = len(notes)

    total = round(time.perf_counter() - t_start, 2)
    result["seconds"] = total
    run_log = {"period": period, "run_at": run_at, "seconds": total, "blocking": blocking, "steps": log,
               "reconciliations": rec_summary.to_dict(orient="records"),
               "dq_warnings": int((dq["severity"] == "warning").sum()), "dq_errors": int((dq["severity"] == "error").sum())}
    if report:
        (out_dir / "run_log.json").write_text(json.dumps(run_log, indent=2, default=str))
    wh.close()
    say(f"\nClose {'BLOCKED' if blocking else 'complete'} in {total:.1f}s")
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--period", required=True, help="YYYY-MM")
    ap.add_argument("--no-report", action="store_true")
    a = ap.parse_args()
    res = run(a.period, report=not a.no_report)
    print("\nReconciliations:")
    print(res["recs"][["reconciliation", "status", "residual", "open_items"]].to_string(index=False))
    sys.exit(1 if res["blocking"] else 0)


if __name__ == "__main__":
    main()
