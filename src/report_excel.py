"""Formatted Excel management pack (openpyxl)."""
from pathlib import Path

import pandas as pd
from openpyxl.formatting.rule import CellIsRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

NAVY, LIGHT, GREEN, RED, AMBER, GREY = "0B1F3A", "EEF2F7", "1E8E5A", "C0392B", "B7791F", "6B7280"
HDR_FILL = PatternFill("solid", fgColor=NAVY)
HDR_FONT = Font(color="FFFFFF", bold=True)
BOLD = Font(bold=True)
THIN = Border(bottom=Side(style="thin", color="D1D5DB"))
TOTAL = Border(top=Side(style="thin", color="111827"), bottom=Side(style="double", color="111827"))
MONEY, PCT = '#,##0;[Red](#,##0);"-"', '+0.0%;[Red]-0.0%;"-"'


def _sheet(ws, df: pd.DataFrame, title: str, subtitle: str, money=(), pct=(), widths=None, start=4):
    ws["A1"] = title
    ws["A1"].font = Font(size=14, bold=True, color=NAVY)
    ws["A2"] = subtitle
    ws["A2"].font = Font(italic=True, color=GREY)
    for j, col in enumerate(df.columns, 1):
        c = ws.cell(row=start, column=j, value=col)
        c.fill, c.font = HDR_FILL, HDR_FONT
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    for i, row in enumerate(df.itertuples(index=False), start + 1):
        for j, v in enumerate(row, 1):
            c = ws.cell(row=i, column=j, value=None if (isinstance(v, float) and pd.isna(v)) else v)
            c.border = THIN
            name = df.columns[j - 1]
            if name in money:
                c.number_format = MONEY
            elif name in pct:
                c.number_format = PCT
    ws.freeze_panes = ws.cell(row=start + 1, column=1)
    for j, col in enumerate(df.columns, 1):
        w = (widths or {}).get(col) or min(max(len(str(col)), *(len(str(v)) for v in df[col].head(200))) + 2, 60)
        ws.column_dimensions[get_column_letter(j)].width = w
    ws.row_dimensions[start].height = 30
    return start + len(df)


def write(r: dict, path: Path) -> Path:
    period, company = r["period"], r["company"]
    sub = f"{company} · period {period} · generated {r['run_at']}"
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        wb = xw.book

        # --- Summary
        ws = wb.create_sheet("Summary")
        k = r["kpis"]
        ws["A1"] = f"{company} · Month-end close {period}"
        ws["A1"].font = Font(size=16, bold=True, color=NAVY)
        ws["A2"] = sub
        ws["A2"].font = Font(italic=True, color=GREY)
        rows = [("Revenue", k["revenue"], MONEY), ("Revenue vs budget", k["revenue_vs_budget_pct"], PCT),
                ("Revenue vs prior month", k["revenue_mom_pct"], PCT), ("Gross margin", k["gross_margin_pct"], "0.0%"),
                ("EBITDA", k["ebitda"], MONEY), ("EBITDA budget", k["ebitda_budget"], MONEY), ("EBITDA margin", k["ebitda_margin_pct"], "0.0%"),
                ("Completed transactions", k["transactions"], "#,##0"), ("Transaction volume (USD)", k["volume_usd"], MONEY),
                ("Average fee (USD)", k["avg_fee_usd"], "0.00"), ("Reversal rate", k["reversal_rate_pct"], "0.0%")]
        ws["A4"], ws["B4"] = "Key figures", ""
        ws["A4"].font = BOLD
        for i, (label, v, fmt) in enumerate(rows, 5):
            ws.cell(row=i, column=1, value=label)
            c = ws.cell(row=i, column=2, value=float(v))
            c.number_format = fmt
        row = 5 + len(rows) + 1
        ws.cell(row=row, column=1, value="Reconciliations").font = BOLD
        for i, rec in enumerate(r["recs"].itertuples(), row + 1):
            ws.cell(row=i, column=1, value=rec.reconciliation)
            c = ws.cell(row=i, column=2, value=rec.status)
            c.font = Font(bold=True, color=GREEN if rec.status != "Unexplained difference" else RED)
        row = i + 2
        dq = r["dq"]
        ws.cell(row=row, column=1, value="Data quality").font = BOLD
        ws.cell(row=row + 1, column=1, value="Warnings")
        ws.cell(row=row + 1, column=2, value=int((dq["severity"] == "warning").sum()))
        ws.cell(row=row + 2, column=1, value="Errors")
        ws.cell(row=row + 2, column=2, value=int((dq["severity"] == "error").sum()))
        ws.cell(row=row + 4, column=1, value="Close status").font = BOLD
        n_adj = int((~r["rec_items"]["category"].str.startswith(("Deposit in transit", "Outstanding payment"))).sum())
        status = "BLOCKED" if r["blocking"] else (f"Reconciled · {n_adj} adjustments to post" if n_adj else "Ready for sign-off")
        c = ws.cell(row=row + 4, column=2, value=status)
        c.font = Font(bold=True, color=RED if r["blocking"] else GREEN)
        ws.column_dimensions["A"].width, ws.column_dimensions["B"].width = 34, 26

        # --- P&L summary
        s = r["summary"].reset_index().rename(columns={"index": "Line"})
        s.columns = ["Line", "Actual (month)", "Budget (month)", "Actual (YTD)", "Budget (YTD)",
                     "Var (month)", "Var % (month)", "Var (YTD)", "Var % (YTD)"]
        s = s[["Line", "Actual (month)", "Budget (month)", "Var (month)", "Var % (month)", "Actual (YTD)", "Budget (YTD)", "Var (YTD)", "Var % (YTD)"]]
        ws = wb.create_sheet("P&L")
        end = _sheet(ws, s, "Profit & loss vs budget", sub + " · variances: positive = favourable",
                     money=[c for c in s.columns if "%" not in c and c != "Line"], pct=["Var % (month)", "Var % (YTD)"], widths={"Line": 24})
        for rr in (7, 9):   # Gross profit, EBITDA rows
            for cc in range(1, len(s.columns) + 1):
                ws.cell(row=rr, column=cc).font = BOLD
                ws.cell(row=rr, column=cc).border = TOTAL

        # --- by account
        a = r["accounts"].copy()
        a = a[["line", "account", "name", "actual_month", "budget_month", "var_month", "var_month_pct", "flag_month",
               "actual_ytd", "budget_ytd", "var_ytd", "var_ytd_pct", "flag_ytd"]]
        a.columns = ["Line", "Account", "Name", "Actual (month)", "Budget (month)", "Var (month)", "Var %", "Flag",
                     "Actual (YTD)", "Budget (YTD)", "Var (YTD)", "Var % YTD", "Flag YTD"]
        a["Line"] = a["Line"].astype(str)
        ws = wb.create_sheet("By account")
        end = _sheet(ws, a, "Budget vs actual by account", sub + " · variance = actual - budget",
                     money=["Actual (month)", "Budget (month)", "Var (month)", "Actual (YTD)", "Budget (YTD)", "Var (YTD)"], pct=["Var %", "Var % YTD"])
        for col in ("H", "M"):
            rng = f"{col}5:{col}{end}"
            ws.conditional_formatting.add(rng, CellIsRule(operator="equal", formula=['"Adverse"'], font=Font(color=RED, bold=True)))
            ws.conditional_formatting.add(rng, CellIsRule(operator="equal", formula=['"Favourable"'], font=Font(color=GREEN, bold=True)))

        # --- by cost centre
        m = r["mart"].groupby(["cost_center", "name"], as_index=False)[["actual_month", "budget_month"]].sum()
        m["variance"] = m["actual_month"] - m["budget_month"]
        m.columns = ["Cost center", "Account", "Actual (month)", "Budget (month)", "Variance"]
        _sheet(wb.create_sheet("By cost center"), m, "Budget vs actual by cost center", sub,
               money=["Actual (month)", "Budget (month)", "Variance"])

        # --- commentary
        cm = pd.DataFrame(r["commentary"]) if r["commentary"] else pd.DataFrame(columns=["account", "name", "flag", "variance", "comment"])
        cm.columns = ["Account", "Name", "Flag", "Variance", "Commentary"]
        ws = wb.create_sheet("Commentary")
        _sheet(ws, cm, "Variance commentary (auto-generated)", sub + " · material = both thresholds breached",
               money=["Variance"], widths={"Commentary": 110})
        for row_ in ws.iter_rows(min_row=5):
            row_[-1].alignment = Alignment(wrap_text=True, vertical="top")

        # --- reconciliations
        rs = r["recs"].copy()
        rs.columns = ["Reconciliation", "Status", "GL balance", "Source balance", "Adjusted source", "Adjusted GL", "Unexplained", "Matched", "Open items"]
        ws = wb.create_sheet("Reconciliations")
        end = _sheet(ws, rs, "Reconciliation summary", sub, money=["GL balance", "Source balance", "Adjusted source", "Adjusted GL", "Unexplained"])
        it = r["rec_items"].copy()
        it["date"] = pd.to_datetime(it["date"], errors="coerce").dt.strftime("%Y-%m-%d").fillna(it["date"].astype(str))
        it.columns = ["Reconciliation", "Reference", "Category", "Date", "GL amount", "Source amount", "Difference", "Explanation"]
        _sheet(wb.create_sheet("Recon items"), it, "Reconciling items", sub, money=["GL amount", "Source amount", "Difference"],
               widths={"Explanation": 90})

        # --- data quality
        d = r["dq"].copy()
        d.columns = ["Source", "Check", "Severity", "Rows", "Action", "Detail"]
        ws = wb.create_sheet("Data quality")
        end = _sheet(ws, d, "Data-quality log", sub, widths={"Detail": 60})
        ws.conditional_formatting.add(f"C5:C{end}", CellIsRule(operator="equal", formula=['"error"'], font=Font(color=RED, bold=True)))
        ws.conditional_formatting.add(f"C5:C{end}", CellIsRule(operator="equal", formula=['"warning"'], font=Font(color=AMBER, bold=True)))

        # --- GL detail (Power BI ready)
        g = r["gl_month"][["je_no", "date", "account", "cost_center", "description", "debit", "credit", "amount", "source"]].copy()
        g["date"] = g["date"].dt.strftime("%Y-%m-%d")
        _sheet(wb.create_sheet("GL detail"), g, "General ledger detail (cleaned)", sub, money=["debit", "credit", "amount"],
               widths={"description": 55})

        if "Sheet" in wb.sheetnames:
            del wb["Sheet"]
    return path
