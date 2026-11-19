"""Generate realistic, deliberately messy source-system extracts for a fictional fintech.

    python generate_data.py

Every month Jan..last_month is clean except the last one, which contains the kind of
issues a real close runs into: duplicate postings, missing cost centres, cut-off errors,
deposits in transit, unrecorded bank charges, a payroll accrual gap and a keying error.
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent
CFG = yaml.safe_load((ROOT / "config.yaml").read_text())
RAW = ROOT / CFG["paths"]["raw"]
rng = np.random.default_rng(2026)

MONTHS = pd.period_range(CFG["generate"]["first_month"], CFG["generate"]["last_month"], freq="M")
ISSUE_MONTH = MONTHS[-1]
LBP_PER_USD = 89_500
BRANCHES = [f"BR{i:02d}" for i in range(1, 13)]
# service: (share of volume, lognormal mu, sigma of USD ticket size)
SERVICES = {"transfer": (0.40, 5.4, 0.7), "bill_pay": (0.33, 4.0, 0.6), "topup": (0.17, 2.8, 0.5), "fx": (0.10, 6.0, 0.6)}
DEPTS = {"Operations": ("OPS", 20, 1400), "Technology": ("TECH", 12, 3200), "Marketing": ("MKT", 5, 2200),
         "Finance": ("FIN", 6, 2500), "HR": ("HR", 3, 2100), "Administration": ("ADMIN", 4, 1600)}
EMPLOYER_RATE = 0.215
OPEN_USD, OPEN_EUR = 2_000_000.0, 400_000.0


def volume(i: int) -> int:
    return int(105_000 * (1 + 0.015 * i) * (1 + 0.06 * np.sin(2 * np.pi * i / 12)))


def fee_usd(service: str, amount_usd: np.ndarray) -> np.ndarray:
    t = CFG["tariff"][service]
    if "flat_usd" in t:
        return np.full_like(amount_usd, t["flat_usd"])
    f = t["pct"] * amount_usd
    return np.maximum(f, t.get("min_usd", 0))


# ------------------------------------------------------------------ reference data
eur = 1.10 + np.cumsum(rng.normal(0, 0.012, 12))
fx_rows = []
for m, r in zip(pd.period_range("2026-01", "2026-12", freq="M"), eur):
    fx_rows += [(str(m), "USD", 1.0), (str(m), "EUR", round(float(r), 4)), (str(m), "LBP", round(1 / LBP_PER_USD, 10))]
FX = pd.DataFrame(fx_rows, columns=["month", "currency", "rate_to_usd"])
RATE = {(m, c): r for m, c, r in fx_rows}

employees = []
k = 1
for dept, (cc, n, mean) in DEPTS.items():
    for _ in range(n):
        employees.append({"Employee ID": f"EMP-{k:04d}", "Department": dept, "Grade": int(rng.integers(1, 6)),
                          "base": round(float(rng.normal(mean, mean * 0.18)), -1)})
        k += 1
NEW_HIRE = {"Employee ID": f"EMP-{k:04d}", "Department": "Technology", "Grade": 4, "base": 3800.0}

# ------------------------------------------------------------------ builders
gl, je_no = [], [100000]


def post(date, desc, ref, lines, source):
    je_no[0] += 1
    label = f"{desc} [{ref}]" if ref else desc
    for acct, cc, dr, cr, ccy, orig in lines:
        gl.append({"JE No": f"JE{je_no[0]}", "Posting Date": date, "Account": acct, "Cost Center": cc,
                   "Description": label, "Debit": round(dr, 2), "Credit": round(cr, 2),
                   "Orig Ccy": ccy, "Orig Amount": round(orig if orig is not None else (dr or cr), 2), "Source": source})


expected_rev = {}   # for the budget: mean fee per transaction by service
sim = {s: fee_usd(s, rng.lognormal(mu, sg, 200_000)).mean() for s, (_, mu, sg) in SERVICES.items()}

post(pd.Timestamp("2026-01-01"), "Opening balances", None,
     [(1000, "FIN", OPEN_USD, 0, "USD", None),
      (1010, "FIN", OPEN_EUR * RATE[("2026-01", "EUR")], 0, "EUR", OPEN_EUR),
      (3000, "FIN", 0, OPEN_USD + OPEN_EUR * RATE[("2026-01", "EUR")], "USD", None)], "ERP")

bank_a_bal, bank_b_bal = OPEN_USD, OPEN_EUR
for i, p in enumerate(MONTHS):
    out = RAW / str(p)
    out.mkdir(parents=True, exist_ok=True)
    issue = p == ISSUE_MONTH
    start, me = p.start_time.normalize(), p.end_time.normalize()
    days = pd.date_range(start, me)
    eur_rate = RATE[(str(p), "EUR")]
    gl_start = 0 if i == 0 else len(gl)   # January export also carries the opening-balance journal
    bank_a, bank_b = [], []          # (date, narrative/ref, amount signed)
    seq = [0]

    # ---------------- platform transactions
    n = volume(i)
    svc = rng.choice(list(SERVICES), n, p=[v[0] for v in SERVICES.values()])
    amt_usd = np.empty(n)
    for s, (_, mu, sg) in SERVICES.items():
        m = svc == s
        amt_usd[m] = rng.lognormal(mu, sg, m.sum())
    fee_u = np.empty(n)
    for s in SERVICES:
        m = svc == s
        fee_u[m] = fee_usd(s, amt_usd[m])
    ccy = rng.choice(["USD", "LBP", "EUR"], n, p=[0.7, 0.2, 0.1])
    rate = np.where(ccy == "USD", 1.0, np.where(ccy == "EUR", eur_rate, 1 / LBP_PER_USD))
    amt_c = amt_usd / rate
    fee_c = fee_u / rate
    lbp = ccy == "LBP"
    amt_c = np.where(lbp, np.round(amt_c, -3), np.round(amt_c, 2))
    fee_c = np.where(lbp, np.round(fee_c, -2), np.round(fee_c, 2))
    fee_eff = fee_c * np.where(ccy == "USD", 1.0, np.where(ccy == "EUR", eur_rate, RATE[(str(p), "LBP")]))
    ts = start + pd.to_timedelta(rng.integers(0, len(days) * 86400, n), unit="s")
    status = np.where(rng.random(n) < 0.02, "reversed", "completed")
    tx = pd.DataFrame({"txn_id": [f"T{p.strftime('%Y%m')}{j:06d}" for j in range(n)], "ts": ts, "service": svc,
                       "branch": rng.choice(BRANCHES, n), "amount": amt_c, "ccy": ccy, "fee": fee_c,
                       "status": status, "_fee_usd": fee_eff})
    done = tx[tx.status == "completed"]
    rev = done.groupby("service")["_fee_usd"].sum()
    daily = done.groupby(done.ts.dt.normalize())["_fee_usd"].sum().reindex(days, fill_value=0)

    # ---------------- revenue journals (month end), with cut-off / reversal errors in the issue month
    for s, acct in CFG["services"].items():
        amount = float(rev.get(s, 0))
        if issue and s == "bill_pay":    # cut-off error: last day's bill-payment revenue not posted
            last = done[(done.service == s) & (done.ts.dt.normalize() == me)]["_fee_usd"].sum()
            amount -= float(last)
        if issue and s == "fx":          # fees on reversed FX deals wrongly recognised
            amount += float(tx[(tx.service == s) & (tx.status == "reversed")]["_fee_usd"].sum())
        post(me, f"Platform revenue - {s.replace('_', ' ')} {p.strftime('%b %Y')}", f"REV-{p.strftime('%Y%m')}-{s.upper()}",
             [(1200, "OPS", amount, 0, "USD", None), (acct, "OPS", 0, amount, "USD", None)], "PLATFORM")

    # ---------------- daily settlements into Bank A
    for d, v in daily.items():
        ref = f"SETTLE-{d.strftime('%Y%m%d')}"
        post(d, "Platform settlement received", ref, [(1000, "OPS", v, 0, "USD", None), (1200, "OPS", 0, v, "USD", None)], "BANKFEED")
        if not (issue and d == me):      # last-day settlement reaches the bank after month end
            bank_a.append((d, f"INWARD SETTLEMENT {ref}", round(v, 2)))

    # ---------------- payments
    def pay(day, acct, cc, usd=None, desc="", bank="A", eur_amt=None, gl_eur=None, in_bank=True, date=None):
        seq[0] += 1
        ref = f"PAY-{p.strftime('%Y%m')}-{seq[0]:03d}"
        d = date if date is not None else start + pd.Timedelta(days=min(day, len(days)) - 1)
        if bank == "A":
            usd = round(usd, 2)
            post(d, desc, ref, [(acct, cc, usd, 0, "USD", None), (1000, cc, 0, usd, "USD", None)], "AP")
            if in_bank:
                bank_a.append((d, f"OUTWARD PMT {ref} {desc.upper()[:24]}", -usd))
        else:
            eur_amt = round(eur_amt, 2)
            booked = gl_eur if gl_eur is not None else eur_amt
            post(d, desc, ref, [(acct, cc, booked * eur_rate, 0, "EUR", booked), (1010, cc, 0, booked * eur_rate, "EUR", booked)], "AP")
            if in_bank:
                bank_b.append((d, ref, desc, -eur_amt))
        return ref

    pay(1, 6100, "ADMIN", 18000, "Office rent")
    pay(5, 6200, "TECH", rng.normal(9000, 1500), "Cloud hosting")
    if issue:   # keying error: EUR 7,425.00 booked as 7,245.00
        pay(8, 6200, "TECH", desc="SaaS licences (EUR)", bank="B", eur_amt=7425.00, gl_eur=7245.00)
    else:
        pay(8, 6200, "TECH", desc="SaaS licences (EUR)", bank="B", eur_amt=rng.normal(7500, 400))
    pay(12, 6500, "ADMIN", rng.normal(4200, 400), "Electricity & internet")
    for d, share in [(6, 0.55), (19, 0.45)]:
        pay(d, 6300, "MKT", rng.normal(24000, 2500) * share, "Digital advertising")
    if issue:
        pay(14, 6300, "MKT", 38000, "Q4 brand campaign - outdoor")
    pay(20, 6400, "FIN", rng.normal(7500, 800), "Legal & advisory retainer")
    if issue:
        pay(22, 6400, "FIN", 22000, "Statutory audit - interim fieldwork")
    for d, cc in [(9, "FIN"), (16, "MKT"), (23, "OPS")]:
        pay(d, 6600, cc, abs(rng.normal(1000, 300)), "Business travel")
    commission = 0.30 * float(rev.get("transfer", 0))
    pay(len(days), 5000, "OPS", commission, "Agent commissions", in_bank=not issue)  # issue month: clears bank in Nov

    # payroll
    staff = [e for e in employees]
    raise_ = 1.03 if p.month >= 7 else 1.0
    reg = [{**{k: v for k, v in e.items() if k != "base"}, "Gross Salary": round(e["base"] * raise_, 2)} for e in staff]
    if issue:
        reg.append({**{k: v for k, v in NEW_HIRE.items() if k != "base"}, "Gross Salary": NEW_HIRE["base"]})
    pr = pd.DataFrame(reg)
    pr["Employer NSSF"] = (pr["Gross Salary"] * EMPLOYER_RATE).round(2)
    pr["Total Cost"] = pr["Gross Salary"] + pr["Employer NSSF"]
    pr["Pay Period"] = p.strftime("%b-%Y")
    gl_pr = pr[pr["Employee ID"] != NEW_HIRE["Employee ID"]] if issue else pr   # new joiner missed in GL
    by_cc = gl_pr.assign(cc=gl_pr["Department"].map(lambda d: DEPTS[d][0])).groupby("cc")["Total Cost"].sum()
    pay_day = start + pd.Timedelta(days=24)
    ref = f"PAYROLL-{p.strftime('%Y%m')}"
    post(pay_day, "Monthly payroll", ref, [(6000, cc, v, 0, "USD", None) for cc, v in by_cc.items()] +
         [(1000, "FIN", 0, float(by_cc.sum()), "USD", None)], "PAYROLL")
    bank_a.append((pay_day, f"PAYROLL BATCH {ref}", -round(float(pr["Total Cost"].sum()), 2)))

    # bank charges (not booked in the issue month)
    charges = round(float(rng.normal(1150, 90)), 2)
    bank_a.append((me, f"MONTHLY ACCOUNT CHARGES {p.strftime('%b').upper()}", -charges))
    if not issue:
        post(me, "Bank charges", f"BANKFEE-{p.strftime('%Y%m')}", [(5100, "FIN", charges, 0, "USD", None), (1000, "FIN", 0, charges, "USD", None)], "BANKFEED")

    # ---------------- data-quality issues in the issue month's GL export
    month_gl = pd.DataFrame(gl[gl_start:])
    if issue:
        travel = month_gl[month_gl["Description"].str.startswith("Business travel")]
        dup = travel[travel["JE No"] == travel["JE No"].iloc[0]].copy()
        dup["JE No"] = f"JE{je_no[0] + 1}"
        je_no[0] += 1
        late = pd.DataFrame([
            {"JE No": f"JE{je_no[0] + 1}", "Posting Date": me + pd.Timedelta(days=2), "Account": 6300, "Cost Center": "MKT",
             "Description": "Influencer campaign deposit [PAY-202611-001]", "Debit": 9500.0, "Credit": 0.0, "Orig Ccy": "USD", "Orig Amount": 9500.0, "Source": "AP"},
            {"JE No": f"JE{je_no[0] + 1}", "Posting Date": me + pd.Timedelta(days=2), "Account": 1000, "Cost Center": "MKT",
             "Description": "Influencer campaign deposit [PAY-202611-001]", "Debit": 0.0, "Credit": 9500.0, "Orig Ccy": "USD", "Orig Amount": 9500.0, "Source": "AP"}])
        je_no[0] += 1
        month_gl = pd.concat([month_gl, dup, late], ignore_index=True)
        util = month_gl.index[month_gl["Description"].str.startswith("Electricity") & (month_gl["Account"] == 6500)]
        # blank the cost centre on a travel line that is NOT part of the duplicated journal
        trav = month_gl.index[month_gl["Description"].str.startswith("Business travel") & (month_gl["Account"] == 6600)
                              & (month_gl["Cost Center"] == "OPS")]
        month_gl.loc[[util[0], trav[0]], "Cost Center"] = ""
    gl_out = month_gl.copy()
    gl_out["Posting Date"] = pd.to_datetime(gl_out["Posting Date"]).dt.strftime("%d/%m/%Y")
    if issue:   # one row exported from a different module with ISO dates
        gl_out.loc[gl_out.index[5], "Posting Date"] = pd.to_datetime(month_gl.loc[month_gl.index[5], "Posting Date"]).strftime("%Y-%m-%d")
    for c in ["Debit", "Credit"]:
        gl_out[c] = gl_out[c].map(lambda v: f"{v:,.2f}" if v else "")
    gl_out.to_csv(out / f"gl_export_{p.strftime('%Y%m')}.csv", index=False)

    # ---------------- platform export (JSON lines) with duplicates and missing fees
    tx_out = tx.drop(columns="_fee_usd").copy()
    tx_out["ts"] = tx_out["ts"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    if issue:
        tx_out = pd.concat([tx_out, tx_out.sample(3, random_state=1)], ignore_index=True)
        tx_out.loc[tx_out.sample(2, random_state=2).index, "fee"] = None
    tx_out.to_json(out / f"transactions_{p.strftime('%Y%m')}.jsonl.gz", orient="records", lines=True, compression="gzip")

    # ---------------- Bank A statement (Excel, US dates, header block)
    ba = pd.DataFrame(sorted(bank_a, key=lambda r: (r[0], r[2] < 0)), columns=["Date", "Narrative", "amt"])
    ba["Debit"] = (-ba["amt"]).clip(lower=0).round(2)
    ba["Credit"] = ba["amt"].clip(lower=0).round(2)
    ba["Balance"] = (bank_a_bal + ba["amt"].cumsum()).round(2)
    opening_a = bank_a_bal
    bank_a_bal = float(ba["Balance"].iloc[-1])
    ba["Date"] = ba["Date"].dt.strftime("%m/%d/%Y")
    with pd.ExcelWriter(out / f"bankA_statement_{p.strftime('%Y%m')}.xlsx") as xw:
        meta = pd.DataFrame([["Bank A - Statement of Account"], ["Account: ****4421 (USD)"],
                             [f"Period: {start:%d/%m/%Y} - {me:%d/%m/%Y}"], [f"Opening balance: {opening_a:,.2f}"]])
        meta.to_excel(xw, index=False, header=False, startrow=0)
        ba[["Date", "Narrative", "Debit", "Credit", "Balance"]].to_excel(xw, index=False, startrow=5)

    # ---------------- Bank B statement (European CSV)
    bb = pd.DataFrame(bank_b, columns=["date", "ref", "desc", "amt"])
    bank_b_bal += bb["amt"].sum()
    eu = lambda v: f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    lines = ["Value Date;Reference;Description;Amount;Currency"]
    lines += [f"{r.date:%Y%m%d};{r.ref};{r.desc};{eu(r.amt)};EUR" for r in bb.itertuples()]
    lines.append(f";CLOSING BALANCE;;{eu(bank_b_bal)};EUR")
    (out / f"bankB_statement_{p.strftime('%Y%m')}.csv").write_text("\n".join(lines), encoding="utf-8")

    # ---------------- payroll register
    pr[["Employee ID", "Department", "Grade", "Gross Salary", "Employer NSSF", "Total Cost", "Pay Period"]].to_excel(
        out / f"payroll_{p.strftime('%Y%m')}.xlsx", index=False)
    print(f"{p}: {n:,} transactions, {len(month_gl)} GL lines, {len(ba)} bank A lines, {len(bb)} bank B lines")

# ------------------------------------------------------------------ budget (FY, wide format) + FX
ref = RAW / "reference"
ref.mkdir(parents=True, exist_ok=True)
FX.to_csv(ref / "fx_rates_2026.csv", index=False)
months = pd.period_range("2026-01", "2026-12", freq="M")
rows = []
for s, acct in CFG["services"].items():
    rows.append([acct, "OPS"] + [round(volume(i) * SERVICES[s][0] * 0.98 * sim[s] * 1.04, -2) for i in range(12)])
rows.append([5000, "OPS"] + [round(volume(i) * 0.40 * 0.98 * sim["transfer"] * 1.04 * 0.30, -2) for i in range(12)])
rows.append([5100, "FIN"] + [1100] * 12)
for dept, (cc, n, mean) in DEPTS.items():
    base = sum(e["base"] for e in employees if e["Department"] == dept)
    rows.append([6000, cc] + [round(base * (1.03 if m.month >= 7 else 1) * (1 + EMPLOYER_RATE), -2) for m in months])
rows += [[6100, "ADMIN"] + [18000] * 12, [6200, "TECH"] + [17500] * 12, [6300, "MKT"] + [25000] * 12,
         [6400, "FIN"] + [7500] * 9 + [7500, 7500, 30000], [6500, "ADMIN"] + [4200] * 12]
for cc in ["FIN", "MKT", "OPS"]:
    rows.append([6600, cc] + [1000] * 12)
budget = pd.DataFrame(rows, columns=["Account", "Cost Center"] + [m.strftime("%b") for m in months])
budget.to_excel(ref / "budget_FY2026.xlsx", index=False)
print(f"Reference data written to {ref}")
