"""Data-quality rules. Each cleaner returns (clean_df, issues) and never silently drops data."""
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd


@dataclass
class Issue:
    source: str
    check: str
    severity: str        # error | warning | info
    rows: int
    action: str
    detail: str = ""


def issues_frame(issues: list[Issue]) -> pd.DataFrame:
    return pd.DataFrame([asdict(i) for i in issues], columns=["source", "check", "severity", "rows", "action", "detail"])


def clean_gl(df: pd.DataFrame, period: str, accounts: set) -> tuple[pd.DataFrame, list[Issue]]:
    issues, src = [], df["source_file"].iloc[0]
    df = df.copy()

    bad_dates = df["date"].isna()
    if bad_dates.any():
        issues.append(Issue(src, "Unparseable posting date", "error", int(bad_dates.sum()), "Excluded", ", ".join(df.loc[bad_dates, "je_no"])))
        df = df[~bad_dates]
    if df["date_format_fallback"].any():
        n = int(df["date_format_fallback"].sum())
        issues.append(Issue(src, "Inconsistent date format (ISO in dd/mm/yyyy column)", "warning", n, "Parsed with ISO fallback",
                            ", ".join(df.loc[df["date_format_fallback"], "je_no"])))

    # duplicate journals: same lines posted twice under a new JE number
    sig_cols = ["date", "account", "cost_center", "debit", "credit", "description"]
    je_sig = df.groupby("je_no").apply(lambda g: tuple(sorted(map(tuple, g[sig_cols].astype(str).values))), include_groups=False)
    dup_jes = je_sig[je_sig.duplicated(keep="first")].index.tolist()
    if dup_jes:
        n = int(df["je_no"].isin(dup_jes).sum())
        issues.append(Issue(src, "Duplicate journal entry", "warning", n, "Later duplicate excluded", ", ".join(dup_jes)))
        df = df[~df["je_no"].isin(dup_jes)]

    out_of_period = df["date"].dt.to_period("M").astype(str) != period
    if out_of_period.any():
        jes = sorted(df.loc[out_of_period, "je_no"].unique())
        issues.append(Issue(src, "Posting date outside close period (cut-off)", "warning", int(out_of_period.sum()),
                            "Excluded from this period", ", ".join(jes)))
        df = df[~out_of_period]

    missing_cc = df["cost_center"].eq("")
    if missing_cc.any():
        issues.append(Issue(src, "Missing cost center", "warning", int(missing_cc.sum()), "Mapped to UNALLOCATED",
                            ", ".join(df.loc[missing_cc, "je_no"])))
        df.loc[missing_cc, "cost_center"] = "UNALLOCATED"

    unknown = ~df["account"].isin(accounts)
    if unknown.any():
        issues.append(Issue(src, "Unknown GL account", "error", int(unknown.sum()), "Excluded",
                            ", ".join(map(str, df.loc[unknown, "account"].unique()))))
        df = df[~unknown]

    bal = df.groupby("je_no")["amount"].sum().round(2)
    unbalanced = bal[bal.abs() > 0.01]
    if len(unbalanced):
        issues.append(Issue(src, "Unbalanced journal entry", "error", len(unbalanced), "Flagged for review", ", ".join(unbalanced.index)))

    issues.append(Issue(src, "Rows accepted", "info", len(df), "Loaded to warehouse"))
    return df.drop(columns=["date_format_fallback"]), issues


def clean_transactions(df: pd.DataFrame, tariff: dict, fx: dict) -> tuple[pd.DataFrame, list[Issue]]:
    """fx: {currency: rate_to_usd} for the period."""
    issues, df = [], df.copy()
    dups = df.duplicated(subset="txn_id", keep="first")
    if dups.any():
        issues.append(Issue("platform", "Duplicate transaction id", "warning", int(dups.sum()), "Duplicates removed",
                            ", ".join(df.loc[dups, "txn_id"].head(10))))
        df = df[~dups]
    df["rate_to_usd"] = df["ccy"].map(fx)
    unknown_ccy = df["rate_to_usd"].isna()
    if unknown_ccy.any():
        issues.append(Issue("platform", "Unknown currency", "error", int(unknown_ccy.sum()), "Excluded"))
        df = df[~unknown_ccy]

    missing_fee = df["fee"].isna()
    if missing_fee.any():
        amt_usd = df.loc[missing_fee, "amount"] * df.loc[missing_fee, "rate_to_usd"]
        imputed = []
        for svc, a in zip(df.loc[missing_fee, "service"], amt_usd):
            t = tariff[svc]
            imputed.append(t["flat_usd"] if "flat_usd" in t else max(t["pct"] * a, t.get("min_usd", 0)))
        df.loc[missing_fee, "fee"] = np.array(imputed) / df.loc[missing_fee, "rate_to_usd"].to_numpy()
        issues.append(Issue("platform", "Missing fee", "warning", int(missing_fee.sum()), "Imputed from tariff card",
                            ", ".join(df.loc[missing_fee, "txn_id"])))
    df["fee_usd"] = df["fee"] * df["rate_to_usd"]
    df["amount_usd"] = df["amount"] * df["rate_to_usd"]
    issues.append(Issue("platform", "Rows accepted", "info", len(df), "Loaded to warehouse"))
    return df, issues


def check_bank_balance(df: pd.DataFrame, opening: float, name: str) -> list[Issue]:
    """Statement integrity: opening + running movements must equal each printed balance."""
    expected = (opening + df["amount"].cumsum()).round(2)
    breaks = (expected - df["balance"]).abs() > 0.01
    if breaks.any():
        return [Issue(name, "Running balance break", "error", int(breaks.sum()), "Statement rejected")]
    return [Issue(name, "Running balance continuity", "info", len(df), "Passed")]
