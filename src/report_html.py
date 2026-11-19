"""Email-ready HTML close summary with embedded charts (no external files)."""
import base64
import html
import io
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mtick
import pandas as pd

NAVY, GREEN, RED, AMBER, GREY, LIGHT = "#0b1f3a", "#1e8e5a", "#c0392b", "#b7791f", "#6b7280", "#eef2f7"
plt.rcParams.update({"font.size": 10, "axes.spines.top": False, "axes.spines.right": False, "axes.grid": True,
                     "grid.color": "#e5e7eb", "axes.axisbelow": True, "axes.titleweight": "bold", "axes.titlelocation": "left"})
kfmt = mtick.FuncFormatter(lambda x, _: f"${x/1e3:,.0f}K")


def _png(fig) -> str:
    buf = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format="png", dpi=150)
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


def charts(r: dict) -> dict:
    out = {}
    # 1. revenue & EBITDA trend vs budget
    t = r["trend"].copy()
    t["ebitda"] = t["revenue"] - t["cost_of_revenue"] - t["opex"]
    labels = pd.PeriodIndex(t["period"], freq="M").strftime("%b")
    fig, ax = plt.subplots(figsize=(7.2, 3.4))
    ax.bar(labels, t["revenue"], color="#c7d2fe", label="Revenue")
    ax.plot(labels, t["ebitda"], color=NAVY, marker="o", lw=2, label="EBITDA")
    ax.yaxis.set_major_formatter(kfmt)
    ax.set_title("Revenue and EBITDA, year to date")
    ax.legend(frameon=False, loc="upper center", bbox_to_anchor=(0.5, -0.1), ncol=2)
    out["trend"] = _png(fig)

    # 2. EBITDA bridge: budget -> actual by P&L line
    s = r["summary"]
    steps = [("Budget EBITDA", s.loc["EBITDA", "budget_month"]), ("Revenue", s.loc["Revenue", "var_month"]),
             ("Cost of revenue", s.loc["Cost of revenue", "var_month"]), ("Operating expenses", s.loc["Operating expenses", "var_month"])]
    fig, ax = plt.subplots(figsize=(7.2, 3.2))
    run = 0.0
    for i, (lbl, v) in enumerate(steps):
        if i == 0:
            ax.bar(lbl, v, color=GREY)
            top, run = v, v
        else:
            ax.bar(lbl, v, bottom=run, color=GREEN if v >= 0 else RED)   # floating bar from run to run+v
            top = max(run, run + v)
            run += v
        ax.text(i, top + 2000, f"{'+' if v > 0 and i else ''}{v/1e3:,.1f}K", ha="center", va="bottom", fontsize=9)
    ax.bar("Actual EBITDA", run, color=NAVY)
    ax.text(len(steps), max(run, 0) + 2000, f"{run/1e3:,.1f}K", ha="center", va="bottom", fontsize=9, fontweight="bold")
    ax.axhline(0, color=GREY, lw=0.8)
    lo, hi = ax.get_ylim()
    ax.set_ylim(min(lo, 0), hi * 1.18)       # headroom so value labels clear the title
    ax.yaxis.set_major_formatter(kfmt)
    ax.set_title("EBITDA bridge: budget to actual (month)")
    ax.tick_params(axis="x", labelsize=8.5)
    out["bridge"] = _png(fig)
    return out


def _money(v):
    return f"−${abs(v):,.0f}" if v < 0 else f"${v:,.0f}"


def write(r: dict, path: Path) -> Path:
    k, s, period = r["kpis"], r["summary"], r["period"]
    img = charts(r)
    status_ok = not r["blocking"]
    n_adj = int((~r["rec_items"]["category"].str.startswith(("Deposit in transit", "Outstanding payment"))).sum())
    status_text = ("BLOCKED: action required" if not status_ok else
                   f"Fully reconciled · {n_adj} adjustments to post" if n_adj else "Ready for sign-off")
    tiles = [("Revenue", _money(k["revenue"]), f"{k['revenue_vs_budget_pct']:+.1%} vs budget"),
             ("EBITDA", _money(k["ebitda"]), f"budget {_money(k['ebitda_budget'])}"),
             ("Gross margin", f"{k['gross_margin_pct']:.1%}", f"{k['transactions']:,} transactions"),
             ("Close run time", f"{r.get('seconds', 0) or sum(x['seconds'] for x in r['log']):.1f}s", "fully automated")]
    tile_html = "".join(f"<td class='tile'><div class='lbl'>{a}</div><div class='val'>{b}</div><div class='sub'>{c}</div></td>" for a, b, c in tiles)

    pnl_rows = ""
    for line, row in s.iterrows():
        bold = " class='tot'" if line in ("Gross profit", "EBITDA") else ""
        col = GREEN if row["var_month"] >= 0 else RED
        pnl_rows += (f"<tr{bold}><td>{line}</td><td>{_money(row['actual_month'])}</td><td>{_money(row['budget_month'])}</td>"
                     f"<td style='color:{col}'>{_money(row['var_month'])}</td><td>{_money(row['actual_ytd'])}</td>"
                     f"<td style='color:{GREEN if row['var_ytd'] >= 0 else RED}'>{_money(row['var_ytd'])}</td></tr>")

    rec_rows = ""
    for rec in r["recs"].itertuples():
        ok = rec.status != "Unexplained difference"
        icon = "✅" if rec.status == "Reconciled" else ("🟡" if ok else "❌")
        rec_rows += f"<tr><td>{icon} {html.escape(rec.reconciliation)}</td><td>{rec.status}</td><td>{rec.open_items}</td><td>{_money(rec.residual)}</td></tr>"

    actions = "".join(f"<li><b>{html.escape(it.category)}</b> · {html.escape(str(it.ref))}: {html.escape(it.explanation)} "
                      f"<span class='muted'>({_money(it.difference)})</span></li>" for it in r["rec_items"].itertuples()
                      if not it.category.startswith(("Deposit in transit", "Outstanding payment")))
    timing = r["rec_items"][r["rec_items"]["category"].str.startswith(("Deposit in transit", "Outstanding payment"))]
    timing_txt = "; ".join(f"{t.category.lower()} {t.ref} ({_money(abs(t.difference))})" for t in timing.itertuples())
    comments = "".join(f"<li>{html.escape(c['comment'])}</li>" for c in r["commentary"]) or "<li>No material variances.</li>"
    dq = r["dq"][r["dq"]["severity"] != "info"]
    dq_rows = "".join(f"<li><b>{html.escape(d.check)}</b> ({d.rows} rows, {html.escape(d.source)}): {html.escape(d.action)}</li>" for d in dq.itertuples()) or "<li>No issues.</li>"

    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{r['company']} close summary {period}</title>
<style>
body{{margin:0;background:{LIGHT};font-family:Segoe UI,Arial,sans-serif;color:#111827;line-height:1.5}}
.wrap{{max-width:760px;margin:0 auto;padding:24px 16px}}
.card{{background:#fff;border-radius:10px;padding:22px 24px;margin-bottom:16px;border:1px solid #e5e7eb}}
h1{{font-size:20px;margin:0;color:{NAVY}}} h2{{font-size:15px;margin:0 0 10px;color:{NAVY}}}
.muted{{color:{GREY}}} .status{{display:inline-block;padding:4px 12px;border-radius:999px;font-weight:700;font-size:13px;
background:{'#dcfce7' if status_ok else '#fee2e2'};color:{GREEN if status_ok else RED}}}
table{{width:100%;border-collapse:collapse;font-size:13px}} td,th{{padding:7px 8px;border-bottom:1px solid #e5e7eb;text-align:right}}
td:first-child,th:first-child{{text-align:left}} th{{color:{GREY};font-weight:600;font-size:12px}}
tr.tot td{{font-weight:700;border-top:1px solid #111827}}
.tiles{{border-spacing:8px;border-collapse:separate;margin:0 -8px}} .tile{{background:{LIGHT};border-radius:8px;text-align:left;vertical-align:top;border:0;width:25%}}
.lbl{{font-size:11px;color:{GREY};text-transform:uppercase;letter-spacing:.04em}} .val{{font-size:20px;font-weight:700;color:{NAVY}}} .sub{{font-size:12px;color:{GREY}}}
li{{margin-bottom:6px;font-size:13px}} img{{width:100%;height:auto}}
@media(max-width:600px){{.tiles td{{display:block;width:auto;margin-bottom:8px}}}}
</style></head><body><div class="wrap">
<div class="card"><h1>{r['company']} · month-end close {pd.Period(period).strftime('%B %Y')}</h1>
<p class="muted" style="margin:4px 0 12px">Automated close run {r['run_at']} · synthetic demo data</p>
<span class="status">{status_text}</span>
<table class="tiles" style="margin-top:14px"><tr>{tile_html}</tr></table></div>

<div class="card"><h2>Profit &amp; loss</h2>
<table><tr><th></th><th>Actual</th><th>Budget</th><th>Variance</th><th>YTD actual</th><th>YTD variance</th></tr>{pnl_rows}</table>
<p class="muted" style="font-size:12px">Variances: positive = favourable.</p>
<img alt="EBITDA bridge" src="data:image/png;base64,{img['bridge']}"></div>

<div class="card"><h2>What drove the month</h2><ul>{comments}</ul>
<img alt="Revenue and EBITDA trend" src="data:image/png;base64,{img['trend']}"></div>

<div class="card"><h2>Reconciliations</h2>
<table><tr><th>Reconciliation</th><th>Status</th><th>Open items</th><th>Unexplained</th></tr>{rec_rows}</table>
<h2 style="margin-top:16px">Adjustments to post before sign-off</h2><ul>{actions}</ul>
<p class="muted" style="font-size:12px">Timing items, which clear next month: {timing_txt or 'none'}.</p></div>

<div class="card"><h2>Data quality</h2><ul>{dq_rows}</ul></div>
<p class="muted" style="text-align:center;font-size:12px">Full detail in the Excel management pack.</p>
</div></body></html>"""
    path.write_text(doc, encoding="utf-8")
    return path
