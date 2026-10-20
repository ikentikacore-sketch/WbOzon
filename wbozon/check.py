"""Сверка WB за последний закрытый период 16–15.

1. Еженедельные отчёты: итоги из списка отчётов (= таблица «Финансовые отчёты» в ЛК,
   /api/finance/v1/sales-reports/list) против сумм по строкам детализации.
2. Период 16–15: продано штук и реализация до СПП по детализации против оперативной статистики продаж.
Результат: data/check_<период>.csv и строки в log.txt.
"""
import datetime as dt

import pandas as pd

from . import wb
from .common import DATA, RAW, log, period_bounds, period_of, read_csv, request, write_csv, yesterday
from .finance import wb_enrich


def last_closed_period():
    p = period_of(yesterday().isoformat())
    a, _ = period_bounds(p)
    return period_of((a - dt.timedelta(days=1)).isoformat())


def run():
    p = last_closed_period()
    a, b = period_bounds(p)
    real = wb_enrich(read_csv(RAW / "wb_realization.csv"))
    rows = []

    # 1. отчёты, пересекающиеся с периодом
    lst = request("POST", f"{wb.FINANCE}/api/finance/v1/sales-reports/list", headers=wb._headers(),
                  body={"dateFrom": (a - dt.timedelta(days=7)).isoformat(), "dateTo": b.isoformat(),
                        "period": "weekly"}) or []
    for r in lst:
        part = real[real["report_id"] == str(r["reportId"])]
        for name, api_key, col in (("Продажа (retailAmount)", "retailAmountSum", "sales"),
                                   ("К перечислению (forPay)", "forPaySum", "to_transfer"),
                                   ("Логистика", "deliveryServiceSum", "logistics"),
                                   ("Хранение", "paidStorageSum", "storage"),
                                   ("Штрафы", "penaltySum", "penalty"),
                                   ("Удержания", "deductionSum", "deduction")):
            lk, ours = float(r.get(api_key) or 0), round(float(part[col].sum()), 2)
            rows.append({"check": f"отчёт {r['reportId']} {r['dateFrom']}–{r['dateTo']} тип {r.get('reportType')}",
                         "metric": name, "lk_report": lk, "ours": ours, "diff": round(ours - lk, 2),
                         "diff_pct": round((ours - lk) / lk * 100, 3) if lk else ""})

    # 2. период 16–15
    rp = real[real["period"] == p]
    sales = read_csv(DATA / "wb_sales.csv")
    st = sales[(sales["date"] >= a.isoformat()) & (sales["date"] <= b.isoformat())].copy()
    for c in ("qty", "revenue"):
        st[c] = pd.to_numeric(st[c])
    st_sold = st[st["status"] == "sale"]
    st_ret = st[st["status"] == "return"]
    sale_by_dt = real[(real["sale_dt"].str[:10] >= a.isoformat()) & (real["sale_dt"].str[:10] <= b.isoformat())]
    pairs = [
        ("продано, шт (реализация по rr_date vs статистика)", rp["qty_sold"].sum(), st_sold["qty"].sum()),
        ("возвраты, шт", rp["qty_returned"].sum(), st_ret["qty"].sum()),
        ("реализация до СПП, руб (продажи − возвраты)", rp["realization_pre_spp"].sum(),
         st_sold["revenue"].sum() - st_ret["revenue"].sum()),
        ("продано, шт (реализация по дате продажи saleDt vs статистика)", sale_by_dt["qty_sold"].sum(),
         st_sold["qty"].sum()),
        ("реализация до СПП, руб (по saleDt)", sale_by_dt["realization_pre_spp"].sum(),
         st_sold["revenue"].sum() - st_ret["revenue"].sum()),
    ]
    for name, real_v, stat_v in pairs:
        real_v, stat_v = round(float(real_v), 2), round(float(stat_v), 2)
        rows.append({"check": f"период {p} ({a}–{b})", "metric": name, "lk_report": real_v, "ours": stat_v,
                     "diff": round(stat_v - real_v, 2),
                     "diff_pct": round((stat_v - real_v) / real_v * 100, 3) if real_v else ""})
    rows += ozon_rows(p, a, b)
    df = pd.DataFrame(rows)
    write_csv(df, DATA / f"check_{p}.csv")
    for r in rows:
        log.info("Сверка | %s | %s | эталон %s | наше %s | %s%%", r["check"], r["metric"], r["lk_report"],
                 r["ours"], r["diff_pct"])
    return df


def ozon_rows(p, a, b):
    """Озон: доставленные отправления и возвраты против начислений «Выручка» за тот же период."""
    fin = read_csv(DATA / f"ozon_finance_{p}.csv")
    sales = read_csv(DATA / "ozon_sales.csv")
    if fin is None or sales is None:
        return []
    fin["amount"] = pd.to_numeric(fin["amount"])
    fin["qty"] = pd.to_numeric(fin["qty"], errors="coerce").fillna(0)
    sale = fin[fin["type_id"] == "sale"]
    pos, neg = sale[sale["amount"] > 0], sale[sale["amount"] < 0]
    s = sales[(sales["date"] >= a.isoformat()) & (sales["date"] <= b.isoformat())].copy()
    for c in ("qty", "revenue"):
        s[c] = pd.to_numeric(s[c])
    d, r = s[s["status"] == "delivered"], s[s["status"] == "return"]
    out = []
    for name, ref, ours in (
        ("Озон: продано, шт (начисления vs отправления)", pos["qty"].sum(), d["qty"].sum()),
        ("Озон: выручка по цене продавца, руб", pos["amount"].sum(), d["revenue"].sum()),
        ("Озон: возвраты, шт", neg["qty"].sum(), r["qty"].sum()),
        ("Озон: возвраты, руб", -neg["amount"].sum(), r["revenue"].sum()),
        ("Озон: к перечислению за период (сумма всех начислений), руб", fin["amount"].sum(), fin["amount"].sum()),
    ):
        ref, ours = round(float(ref), 2), round(float(ours), 2)
        out.append({"check": f"Озон период {p} ({a}–{b})", "metric": name, "lk_report": ref, "ours": ours,
                    "diff": round(ours - ref, 2), "diff_pct": round((ours - ref) / ref * 100, 3) if ref else ""})
    return out
