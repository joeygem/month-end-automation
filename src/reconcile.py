"""Automated reconciliations with root-cause hints.

Each reconciliation returns (summary: dict, items: DataFrame). Items use one schema:
reconciliation, ref, category, date, gl_amount, source_amount, difference, explanation.
"""
import numpy as np
import pandas as pd

ITEM_COLS = ["reconciliation", "ref", "category", "date", "gl_amount", "source_amount", "difference", "explanation"]


def _items(rows, name):
    df = pd.DataFrame(rows, columns=ITEM_COLS[1:])
    df.insert(0, "reconciliation", name)
    return df


def _status(residual, tol):
    return "Reconciled" if abs(residual) <= tol else "Unexplained difference"


# ---------------------------------------------------------------- bank
def _match_amount_date(m: pd.DataFrame, days: int) -> tuple[pd.DataFrame, int]:
    """Pair GL-only and bank-only lines with the same amount within `days`; returns rows left and pairs found."""
    gl_only = m.index[m["_merge"] == "left_only"].tolist()
    bank_only = m.index[m["_merge"] == "right_only"].tolist()
    used, drop = set(), []
    for gi in gl_only:
        for bi in bank_only:
            if bi in used:
                continue
            same_amt = abs(m.at[gi, "gl_amount"] - m.at[bi, "source_amount"]) <= 0.01
            close = abs((pd.Timestamp(m.at[gi, "date_gl"]) - pd.Timestamp(m.at[bi, "date_bank"])).days) <= days
            if same_amt and close:
                used.add(bi)
                drop += [gi, bi]
                break
    return m.drop(index=drop), len(drop) // 2


def bank(gl_all: pd.DataFrame, statement: pd.DataFrame, closing_bank: float, account: int, period: str,
         name: str, tol: float, use_orig: bool = False) -> tuple[dict, pd.DataFrame]:
    """Match GL cash lines to bank lines by reference and build the reconciliation statement.

    adjusted bank = bank balance + deposits in transit - outstanding payments
    adjusted book = GL balance + bank-only items + amount corrections
    """
    amt = "orig_amount_signed" if use_orig else "amount"
    gl = gl_all[gl_all["account"] == account].copy()
    if use_orig:
        gl[amt] = np.sign(gl["amount"]) * gl["orig_amount"]
    gl_close = round(float(gl[amt].sum()), 2)
    g = gl[gl["period"] == period].groupby("ref", dropna=True).agg(gl_amount=(amt, "sum"), date=("date", "min")).reset_index()
    b = statement.groupby("ref", dropna=False).agg(source_amount=("amount", "sum"), date=("date", "min"),
                                                   narrative=("narrative", "first")).reset_index()
    m = g.merge(b, on="ref", how="outer", suffixes=("_gl", "_bank"), indicator=True)

    # Pass 2: bank lines without a usable reference (e.g. charges) are matched on amount + date (+/- 3 days)
    m, fuzzy = _match_amount_date(m, days=3)

    rows, adj_bank, adj_book, matched = [], 0.0, 0.0, fuzzy
    for r in m.to_dict("records"):
        side = r["_merge"]
        if side == "both":
            diff = round(r["source_amount"] - r["gl_amount"], 2)
            if abs(diff) <= 0.01:
                matched += 1
                continue
            adj_book += diff
            rows.append((r["ref"], "Amount difference (book error)", r["date_gl"], r["gl_amount"], r["source_amount"], diff,
                         "Bank amount differs from the amount posted in the GL: correct the journal"))
        elif side == "left_only":              # in GL, not on the statement -> timing
            adj_bank += r["gl_amount"]
            if r["gl_amount"] > 0:
                cat, why = "Deposit in transit", "Recorded in GL, not yet credited by the bank (timing)"
            else:
                cat, why = "Outstanding payment", "Paid in GL, not yet cleared by the bank (timing)"
            rows.append((r["ref"], cat, r["date_gl"], r["gl_amount"], 0.0, -r["gl_amount"], why))
        else:                                  # on the statement, not in GL -> book adjustment
            narrative = str(r["narrative"]).upper()
            adj_book += r["source_amount"]
            if "CHARGE" in narrative or "FEE" in narrative:
                cat, why = "Unrecorded bank charges", "On the statement but not booked: post Dr 5100 Bank charges"
            else:
                cat, why = "Unrecorded bank item", f"On the statement but not in GL: investigate ({r['narrative']})"
            ref = r["ref"] if isinstance(r["ref"], str) else str(r["narrative"]).strip()[:32]
            rows.append((ref, cat, r["date_bank"], 0.0, r["source_amount"], r["source_amount"], why))

    adjusted_bank = round(closing_bank + adj_bank, 2)
    adjusted_book = round(gl_close + adj_book, 2)
    residual = round(adjusted_bank - adjusted_book, 2)
    summary = {"reconciliation": name, "status": _status(residual, tol), "gl_balance": gl_close, "source_balance": round(closing_bank, 2),
               "adjusted_source": adjusted_bank, "adjusted_gl": adjusted_book, "residual": residual,
               "matched": matched, "open_items": len(rows)}
    return summary, _items(rows, name)


# ---------------------------------------------------------------- revenue
def revenue(platform: pd.DataFrame, gl: pd.DataFrame, services: dict, period: str, tol: float) -> tuple[dict, pd.DataFrame]:
    """Platform fee income (completed transactions) vs GL revenue by service, with root-cause hints."""
    done = platform[platform["status"] == "completed"]
    last_day = platform["ts"].dt.normalize().max()
    rows, total_diff, total_src, total_gl = [], 0.0, 0.0, 0.0
    for svc, acct in services.items():
        src = round(float(done.loc[done["service"] == svc, "fee_usd"].sum()), 2)
        booked = round(float(-gl.loc[(gl["period"] == period) & (gl["account"] == acct), "amount"].sum()), 2)
        diff = round(booked - src, 2)
        total_src += src; total_gl += booked
        if abs(diff) <= tol:
            continue
        total_diff += diff
        last = float(done.loc[(done["service"] == svc) & (done["ts"].dt.normalize() == last_day), "fee_usd"].sum())
        rev = float(platform.loc[(platform["service"] == svc) & (platform["status"] == "reversed"), "fee_usd"].sum())
        if abs(-diff - last) <= tol:
            why = f"Equals fees on {last_day:%d %b} ({last:,.2f}): last day of the month not posted (cut-off error)"
            cat = "Cut-off error"
        elif abs(diff - rev) <= tol:
            why = f"Equals fees on reversed transactions ({rev:,.2f}): reversals recognised as revenue"
            cat = "Reversals booked as revenue"
        else:
            why = "No automatic match: investigate journal and platform settlement"
            cat = "Unexplained"
        rows.append((f"{acct} {svc}", cat, period, booked, src, diff, why))
    unexplained = sum(r[5] for r in rows if r[1] == "Unexplained")
    summary = {"reconciliation": "Revenue: platform vs GL", "status": "Reconciled" if not rows else ("Explained differences" if abs(unexplained) <= tol else "Unexplained difference"),
               "gl_balance": round(total_gl, 2), "source_balance": round(total_src, 2), "adjusted_source": round(total_src, 2),
               "adjusted_gl": round(total_gl - total_diff, 2), "residual": round(unexplained, 2), "matched": len(services) - len(rows), "open_items": len(rows)}
    return summary, _items(rows, "Revenue: platform vs GL")


# ---------------------------------------------------------------- payroll
def payroll(register: pd.DataFrame, gl: pd.DataFrame, period: str, tol: float) -> tuple[dict, pd.DataFrame]:
    reg = register.groupby("cost_center")["total_cost"].sum().round(2)
    booked = gl[(gl["period"] == period) & (gl["account"] == 6000)].groupby("cost_center")["amount"].sum().round(2)
    rows = []
    for cc in sorted(set(reg.index) | set(booked.index)):
        r, b = float(reg.get(cc, 0)), float(booked.get(cc, 0))
        diff = round(b - r, 2)
        if abs(diff) <= tol:
            continue
        emp = register[(register["cost_center"] == cc) & ((register["total_cost"] + diff).abs() <= tol)]
        if len(emp):
            e = emp.iloc[0]
            cat, why = "Employee not accrued", f"Equals total cost of {e['employee_id']} ({e['department']}): likely a new joiner missing from the GL journal"
        else:
            cat, why = "Unexplained", "Payroll register and GL differ: review journal"
        rows.append((cc, cat, period, b, r, diff, why))
    unexplained = sum(r[5] for r in rows if r[1] == "Unexplained")
    summary = {"reconciliation": "Payroll: register vs GL", "status": "Reconciled" if not rows else ("Explained differences" if abs(unexplained) <= tol else "Unexplained difference"),
               "gl_balance": round(float(booked.sum()), 2), "source_balance": round(float(reg.sum()), 2),
               "adjusted_source": round(float(reg.sum()), 2), "adjusted_gl": round(float(booked.sum()) - sum(r[5] for r in rows), 2),
               "residual": round(unexplained, 2), "matched": len(reg) - len(rows), "open_items": len(rows)}
    return summary, _items(rows, "Payroll: register vs GL")
