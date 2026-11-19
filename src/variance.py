"""Budget vs actual: P&L layout, materiality flags and auto-generated commentary."""
import numpy as np
import pandas as pd

LINES = ["Revenue", "Cost of revenue", "Operating expenses"]


def flag(actual: float, budget: float, is_revenue: bool, abs_thr: float, pct_thr: float) -> tuple[float, float, str]:
    var = actual - budget
    pct = var / budget if budget else np.nan
    favourable = var >= 0 if is_revenue else var <= 0
    material = abs(var) >= abs_thr and (np.isnan(pct) or abs(pct) >= pct_thr)
    return var, pct, ("" if not material else ("Favourable" if favourable else "Adverse"))


def pnl_by_account(mart: pd.DataFrame, abs_thr: float, pct_thr: float) -> pd.DataFrame:
    acc = mart.groupby(["line", "account", "name", "type"], as_index=False)[["actual_month", "budget_month", "actual_ytd", "budget_ytd"]].sum()
    out = []
    for r in acc.itertuples():
        rev = r.type == "revenue"
        vm, pm, fm = flag(r.actual_month, r.budget_month, rev, abs_thr, pct_thr)
        vy, py, fy = flag(r.actual_ytd, r.budget_ytd, rev, abs_thr * 3, pct_thr / 2)
        out.append({"line": r.line, "account": r.account, "name": r.name, "type": r.type,
                    "actual_month": r.actual_month, "budget_month": r.budget_month, "var_month": vm, "var_month_pct": pm, "flag_month": fm,
                    "actual_ytd": r.actual_ytd, "budget_ytd": r.budget_ytd, "var_ytd": vy, "var_ytd_pct": py, "flag_ytd": fy})
    df = pd.DataFrame(out)
    df["line"] = pd.Categorical(df["line"], LINES, ordered=True)
    return df.sort_values(["line", "account"]).reset_index(drop=True)


def summary_lines(acc: pd.DataFrame) -> pd.DataFrame:
    """Revenue, Cost of revenue, Gross profit, Opex, EBITDA for month and YTD."""
    cols = ["actual_month", "budget_month", "actual_ytd", "budget_ytd"]
    s = acc.groupby("line", observed=True)[cols].sum()
    gp = s.loc["Revenue"] - s.loc["Cost of revenue"]
    ebitda = gp - s.loc["Operating expenses"]
    t = pd.DataFrame([s.loc["Revenue"], s.loc["Cost of revenue"], gp, s.loc["Operating expenses"], ebitda],
                     index=["Revenue", "Cost of revenue", "Gross profit", "Operating expenses", "EBITDA"])
    for p in ["month", "ytd"]:
        t[f"var_{p}"] = t[f"actual_{p}"] - t[f"budget_{p}"]
        t.loc[["Cost of revenue", "Operating expenses"], f"var_{p}"] *= -1   # positive = favourable
        t[f"var_{p}_pct"] = t[f"var_{p}"] / t[f"budget_{p}"].abs()
    return t


def commentary(acc: pd.DataFrame, mart: pd.DataFrame, gl_month: pd.DataFrame, period: str) -> list[dict]:
    """Plain-English explanation for each materially varying account, using the largest journals as drivers."""
    notes = []
    flagged = acc[acc["flag_month"] != ""].copy()
    flagged["abs_var"] = flagged["var_month"].abs()
    for r in flagged.sort_values("abs_var", ascending=False).itertuples():
        direction = "above" if r.var_month > 0 else "below"
        cc = mart[mart["account"] == r.account].assign(v=lambda d: d.actual_month - d.budget_month)
        top_cc = cc.loc[cc["v"].abs().idxmax(), "cost_center"] if len(cc) else "-"
        lines = gl_month[gl_month["account"] == r.account].copy()
        lines["desc"] = lines["description"].str.replace(r"\s*\[.*\]$", "", regex=True)
        drivers = lines.groupby("desc")["amount"].sum().abs().sort_values(ascending=False).head(2)
        drv = "; ".join(f"{d} (${v:,.0f})" for d, v in drivers.items())
        pct = f"{r.var_month_pct:+.0%}" if pd.notna(r.var_month_pct) else "n/a"
        text = (f"{r.name} was ${abs(r.var_month):,.0f} {direction} budget ({pct}), {r.flag_month.lower()}. "
                f"Largest cost-centre impact: {top_cc}. Main postings: {drv}.")
        notes.append({"account": r.account, "name": r.name, "flag": r.flag_month, "variance": r.var_month, "comment": text})
    return notes


def kpis(platform: pd.DataFrame, summary: pd.DataFrame, trend: pd.DataFrame) -> dict:
    done = platform[platform["status"] == "completed"]
    rev = summary.loc["Revenue", "actual_month"]
    prev = trend["revenue"].iloc[-2] if len(trend) > 1 else np.nan
    return {
        "revenue": rev,
        "revenue_vs_budget_pct": summary.loc["Revenue", "var_month_pct"],
        "revenue_mom_pct": rev / prev - 1 if prev else np.nan,
        "gross_margin_pct": summary.loc["Gross profit", "actual_month"] / rev,
        "ebitda": summary.loc["EBITDA", "actual_month"],
        "ebitda_margin_pct": summary.loc["EBITDA", "actual_month"] / rev,
        "ebitda_budget": summary.loc["EBITDA", "budget_month"],
        "transactions": int(len(done)),
        "volume_usd": float(done["amount_usd"].sum()),
        "avg_fee_usd": float(done["fee_usd"].mean()),
        "reversal_rate_pct": float((platform["status"] == "reversed").mean()),
    }
