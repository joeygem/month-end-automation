"""Readers for each source system. Each returns a tidy DataFrame with standard column names."""
import re
from pathlib import Path

import numpy as np
import pandas as pd

REF = re.compile(r"(SETTLE-\d{8}|PAY-\d{6}-\d{3}|PAYROLL-\d{6}|BANKFEE-\d{6}|REV-\d{6}-[A-Z_]+)")


def _num(s: pd.Series) -> pd.Series:
    """'1,234.50' / '' / NaN -> float."""
    return pd.to_numeric(s.astype(str).str.replace(",", "", regex=False).replace({"": "0", "nan": "0"}), errors="coerce").fillna(0.0)


def _eu_num(s: pd.Series) -> pd.Series:
    """'-7.425,00' -> -7425.0"""
    return pd.to_numeric(s.astype(str).str.replace(".", "", regex=False).str.replace(",", ".", regex=False), errors="coerce")


def extract_ref(text: pd.Series) -> pd.Series:
    return text.astype(str).str.extract(REF, expand=False)


def read_gl(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype={"Cost Center": str, "Posting Date": str})
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
    raw_date = df["posting_date"].astype(str)
    df["date"] = pd.to_datetime(raw_date, format="%d/%m/%Y", errors="coerce")
    iso = df["date"].isna() & raw_date.str.match(r"^\d{4}-\d{2}-\d{2}$")
    df.loc[iso, "date"] = pd.to_datetime(raw_date[iso], format="%Y-%m-%d")
    df["date_format_fallback"] = iso
    df["debit"], df["credit"] = _num(df["debit"]), _num(df["credit"])
    df["amount"] = (df["debit"] - df["credit"]).round(2)
    df["cost_center"] = df["cost_center"].fillna("").str.strip()
    df["ref"] = extract_ref(df["description"])
    df["source_file"] = path.name
    return df


def read_transactions(path: Path) -> pd.DataFrame:
    df = pd.read_json(path, lines=True, compression="infer", dtype={"txn_id": str})
    df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_localize(None)
    return df


def read_bank_a(path: Path) -> tuple[pd.DataFrame, float]:
    """Excel statement with a free-text header block; finds the real header row."""
    raw = pd.read_excel(path, header=None)
    header_row = raw.index[raw.iloc[:, 0].astype(str).str.strip().eq("Date")][0]
    opening_line = raw.iloc[:header_row, 0].astype(str)
    opening = float(opening_line[opening_line.str.startswith("Opening balance")].iloc[0].split(":")[1].replace(",", ""))
    df = raw.iloc[header_row + 1:].copy()
    df.columns = [str(c).strip().lower() for c in raw.iloc[header_row]]
    df = df.dropna(subset=["date"])
    df["date"] = pd.to_datetime(df["date"], format="%m/%d/%Y")
    for c in ["debit", "credit", "balance"]:
        df[c] = pd.to_numeric(df[c]).astype(float)
    df["amount"] = (df["credit"] - df["debit"]).round(2)
    df["ref"] = extract_ref(df["narrative"])
    return df.reset_index(drop=True), opening


def read_bank_b(path: Path) -> tuple[pd.DataFrame, float]:
    """European CSV: semicolons, decimal commas, YYYYMMDD dates, closing-balance trailer."""
    df = pd.read_csv(path, sep=";", dtype=str)
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
    trailer = df["reference"].str.upper().eq("CLOSING BALANCE")
    closing = float(_eu_num(df.loc[trailer, "amount"]).iloc[0])
    df = df[~trailer].copy()
    df["date"] = pd.to_datetime(df["value_date"], format="%Y%m%d")
    df["amount"] = _eu_num(df["amount"]).round(2)
    df["ref"] = extract_ref(df["reference"])
    df["narrative"] = df["description"]
    return df.reset_index(drop=True), closing


def read_payroll(path: Path, departments: dict) -> pd.DataFrame:
    df = pd.read_excel(path)
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
    df["cost_center"] = df["department"].map(departments)
    return df


def read_budget(path: Path, year: int) -> pd.DataFrame:
    wide = pd.read_excel(path)
    long = wide.melt(id_vars=["Account", "Cost Center"], var_name="mon", value_name="budget")
    long["period"] = pd.to_datetime(long["mon"] + f" {year}", format="%b %Y").dt.to_period("M").astype(str)
    return long.rename(columns={"Account": "account", "Cost Center": "cost_center"})[["period", "account", "cost_center", "budget"]]


def read_fx(path: Path) -> pd.DataFrame:
    return pd.read_csv(path)


def month_files(raw: Path, period: str) -> dict:
    d = raw / period
    ym = period.replace("-", "")
    files = {
        "gl": d / f"gl_export_{ym}.csv",
        "transactions": next(iter(sorted(d.glob(f"transactions_{ym}.jsonl*"))), d / f"transactions_{ym}.jsonl.gz"),
        "bank_a": d / f"bankA_statement_{ym}.xlsx",
        "bank_b": d / f"bankB_statement_{ym}.csv",
        "payroll": d / f"payroll_{ym}.xlsx",
    }
    missing = [k for k, v in files.items() if not v.exists()]
    if missing:
        raise FileNotFoundError(f"{period}: missing source files {missing} in {d}")
    return files
