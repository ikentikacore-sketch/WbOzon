"""Отчёт для пересмотра плана Озона: заказы, выкупы, остатки, дни без остатка, сводка темпа.

    python run.py --ozon-plan                     # БЖ2, БЖ4, БЖ5, БЖ11 с 01.02.2026 по вчера
    python run.py --ozon-plan "БЖ2,БЖ11"

Перед запуском нужна свежая выгрузка Озона (python run.py --only ozon).
Результат: data/ozon_plan_<артикулы>_<дата>.xlsx (4 листа) и сверка в логе.

Все даты — по московскому времени (UTC+3).
* Заказы — /v3/posting/fbo/list и /v4/posting/fbs/list, дата = created_at по Москве.
* Итоговый статус заказа: доставлено / отменено / в пути; «возврат» — если по отправлению есть
  возврат покупателя (/v1/returns/list, тип ClientReturn).
* Выкупы по дате доставки — fact_delivery_date из /v2/posting/fbo/get и /v3/posting/fbs/get.
* Остатки — /v4/product/info/stocks (на складах), /v1/analytics/stocks (в пути на склад),
  отправления в статусах delivering/awaiting_deliver (в пути к покупателю).
* Истории остатков по дням Seller API не отдаёт: дни без остатка — только по ежедневным срезам скрипта.
"""
import datetime as dt
import re

import pandas as pd
from openpyxl.utils import get_column_letter

from . import ozon
from .common import DATA, log, read_csv, yesterday

DEFAULT = ["БЖ2", "БЖ4", "БЖ5", "БЖ11"]
START = dt.date(2026, 2, 1)
IN_TRANSIT = {"delivering", "awaiting_deliver", "awaiting_packaging", "awaiting_registration",
              "acceptance_in_progress", "driver_pickup", "sent_by_seller"}


def _size_key(s):
    m = re.match(r"\d+", str(s))
    return int(m.group()) if m else 0


def _orders(arts):
    o = read_csv(DATA / "ozon_orders.csv")
    o = o[o["model"].isin(arts)].copy()
    o["qty"] = pd.to_numeric(o["qty"])
    s = read_csv(DATA / "ozon_sales.csv")
    ret = s[(s["status"] == "return")]
    returned = set(zip(ret["posting_number"], ret["sku"].astype(str)))
    o["returned"] = [(p, str(k)) in returned for p, k in zip(o["posting_number"], o["sku"])]

    def final(r):
        if r["status"] == "delivered":
            return "возврат" if r["returned"] else "доставлено"
        if r["status"] == "cancelled":
            return "отменено"
        if r["status"] in IN_TRANSIT:
            return "в пути"
        return r["status"]
    o["final"] = o.apply(final, axis=1)
    return o


def orders_sheet(o, dd):
    keys = ["date", "model", "size", "scheme"]
    a = o[(o["date"] >= START.isoformat()) & (o["date"] <= yesterday().isoformat())]
    g = a.pivot_table(index=keys, columns="final", values="qty", aggfunc="sum", fill_value=0)
    for c in ("доставлено", "отменено", "возврат", "в пути"):
        if c not in g:
            g[c] = 0
    g["Заказано, шт"] = g.sum(axis=1)
    # выкупы по дате доставки (не по дате заказа): доставленные, включая потом возвращённые
    d = o[o["status"] == "delivered"].copy()
    d["date"] = d["posting_number"].map(dd).fillna("")
    d = d[(d["date"] >= START.isoformat()) & (d["date"] <= yesterday().isoformat())]
    b = d.groupby(keys)["qty"].sum().rename("Выкуплено в этот день (по дате доставки), шт")
    out = g.join(b, how="outer").fillna(0).reset_index()
    out = out.rename(columns={"date": "Дата", "model": "Артикул", "size": "Размер", "scheme": "Схема",
                              "доставлено": "Доставлено, шт", "отменено": "Отменено, шт",
                              "возврат": "Возврат, шт", "в пути": "В пути, шт"})
    cols = ["Дата", "Артикул", "Размер", "Заказано, шт", "Доставлено, шт", "Отменено, шт", "Возврат, шт",
            "В пути, шт", "Выкуплено в этот день (по дате доставки), шт", "Схема"]
    other = [c for c in out.columns if c not in cols]  # прочие статусы, если появятся
    out = out[cols + other]
    for c in out.columns[3:]:
        if c != "Схема":
            out[c] = out[c].astype(int)
    return out.sort_values(["Дата", "Артикул", "Размер"], key=lambda s: s.map(_size_key) if s.name == "Размер" else s)


def stock_sheet(arts, o):
    day = sorted(DATA.glob("ozon_stock_*.csv"))[-1]
    st = read_csv(day)
    st = st[st["model"].isin(arts)].copy()
    for c in ("present", "in_transit"):
        st[c] = pd.to_numeric(st[c], errors="coerce").fillna(0)
    on = st[st["scheme"].str.endswith("_TOTAL")].groupby(["model", "size", "scheme"])["present"].sum()
    on = on.unstack("scheme", fill_value=0)
    tr = st[st["scheme"] == "FBO"].groupby(["model", "size"])["in_transit"].sum()
    way = o[o["final"] == "в пути"].groupby(["model", "size"])["qty"].sum()
    out = pd.DataFrame(index=on.index)
    out["На складах Ozon (FBO), шт"] = on.get("FBO_TOTAL", 0)
    out["На своём складе (FBS), шт"] = on.get("FBS_TOTAL", 0)
    out["В пути на склад, шт"] = tr.reindex(out.index, fill_value=0)
    out["В пути к покупателю, шт"] = way.reindex(out.index, fill_value=0)
    out = out.fillna(0).astype(int).reset_index().rename(columns={"model": "Артикул", "size": "Размер"})
    out.insert(0, "Дата снятия", day.stem.rsplit("_", 1)[1])
    return out.sort_values(["Артикул", "Размер"], key=lambda s: s.map(_size_key) if s.name == "Размер" else s)


def zero_days_sheet(arts):
    rows = []
    since = (dt.date.today() - dt.timedelta(days=60)).isoformat()
    for f in sorted(DATA.glob("ozon_stock_*.csv")):
        day = f.stem.rsplit("_", 1)[1]
        if day < since:
            continue
        st = read_csv(f)
        st = st[st["model"].isin(arts) & (st["scheme"] == "FBO_TOTAL")]
        st = st.assign(present=pd.to_numeric(st["present"], errors="coerce").fillna(0))
        for (m, s), v in st.groupby(["model", "size"])["present"].sum().items():
            if v == 0:
                rows.append({"Артикул": m, "Размер": s, "Дата, когда остаток был 0": day,
                             "Источник": "ежедневный срез скрипта (остаток FBO на складах Ozon)"})
    return pd.DataFrame(rows, columns=["Артикул", "Размер", "Дата, когда остаток был 0", "Источник"])


def summary_sheet(o, dd, arts):
    end = yesterday()
    d = o[o["status"] == "delivered"].copy()
    d["ddate"] = d["posting_number"].map(dd).fillna("")
    rows = []
    months = pd.period_range(START, end, freq="M").strftime("%Y-%m")
    for scheme in ["FBO+FBS"] + sorted(o["scheme"].unique()):
        for art in arts:
            oa = o[o["model"] == art]
            da = d[d["model"] == art]
            if scheme != "FBO+FBS":
                oa, da = oa[oa["scheme"] == scheme], da[da["scheme"] == scheme]
                if not len(oa):
                    continue
            r = {"Артикул": art, "Схема": scheme}
            for n in (7, 14, 28):
                a = (end - dt.timedelta(days=n - 1)).isoformat()
                r[f"Заказов/день за {n} дн"] = round(oa[(oa["date"] >= a) & (oa["date"] <= end.isoformat())]["qty"].sum() / n, 2)
            for n in (7, 14, 28):
                a = (end - dt.timedelta(days=n - 1)).isoformat()
                r[f"Выкупов/день за {n} дн"] = round(da[(da["ddate"] >= a) & (da["ddate"] <= end.isoformat())]["qty"].sum() / n, 2)
            for m in months:
                om = oa[oa["date"].str[:7] == m]
                n_all = om["qty"].sum()
                r[f"Доля выкупа {m}"] = round(om[om["final"] == "доставлено"]["qty"].sum() / n_all, 3) if n_all else None
                r[f"В пути {m}, шт"] = int(om[om["final"] == "в пути"]["qty"].sum())
            rows.append(r)
    df = pd.DataFrame(rows)
    # колонки «в пути» нужны только для месяцев, где заказы ещё не завершены
    drop = [c for c in df.columns if c.startswith("В пути ") and (df[c] == 0).all()]
    return df.drop(columns=drop)


def check(o, arts):
    """Сверка заказанных штук за 28 дней с аналитикой Озона (/v1/analytics/data, ordered_units —
    те же данные, что в ЛК «Аналитика → Графики»)."""
    end = yesterday()
    a = end - dt.timedelta(days=27)
    r = ozon.post("/v1/analytics/data", {"date_from": a.isoformat(), "date_to": end.isoformat(),
                                         "metrics": ["ordered_units"], "dimension": ["sku"],
                                         "filters": [], "limit": 1000, "offset": 0})
    products = ozon.load_json(ozon.RAW / "ozon_products.json", {})
    art_by_sku = {str(ozon.sku_of(p)): ozon.ozon_model(off) for off, p in products.items()}
    lk = {}
    for x in (r.get("result") or {}).get("data") or []:
        art = art_by_sku.get(str(x["dimensions"][0]["id"]))
        if art in arts:
            lk[art] = lk.get(art, 0) + x["metrics"][0]
    rows = []
    for art in arts:
        ours = int(o[(o["model"] == art) & (o["date"] >= a.isoformat()) & (o["date"] <= end.isoformat())]["qty"].sum())
        ref = lk.get(art, 0)
        rows.append({"Артикул": art, "Период": f"{a}–{end}", "Аналитика Ozon (ordered_units)": ref,
                     "Заказы по отправлениям": ours, "Расхождение, шт": ours - ref,
                     "Расхождение, %": round((ours - ref) / ref * 100, 2) if ref else None})
    return pd.DataFrame(rows)


def build(arts=None):
    arts = [a.strip().upper() for a in (arts or DEFAULT) if a.strip()]
    o = _orders(arts)
    dlv = o[(o["status"] == "delivered") & (o["date"] >= (START - dt.timedelta(days=30)).isoformat())]
    dd = ozon.delivery_dates(list(zip(dlv["posting_number"], dlv["scheme"])))
    sheets = {
        "Заказы и выкупы": orders_sheet(o, dd),
        "Остатки": stock_sheet(arts, o),
        "Дни без остатка": zero_days_sheet(arts),
        "Сводка": summary_sheet(o, dd, arts),
    }
    chk = check(o, arts)
    tag = "_".join(arts) if len(arts) <= 6 else f"{len(arts)}art"
    out = DATA / f"ozon_plan_{tag}_{dt.date.today().isoformat()}.xlsx"
    with pd.ExcelWriter(out, engine="openpyxl") as xw:
        for name, df in sheets.items():
            df.to_excel(xw, sheet_name=name, index=False)
            ws = xw.sheets[name]
            for i, col in enumerate(ws.iter_cols(min_row=1, max_row=min(ws.max_row, 300)), 1):
                ws.column_dimensions[get_column_letter(i)].width = min(max(len(str(c.value or "")) for c in col) + 2, 45)
            ws.freeze_panes = "A2"
    chk.to_csv(out.with_name(out.stem + "_check.csv"), index=False, encoding="utf-8-sig")
    log.info("Отчёт для плана Озона: %s", out.name)
    return out, sheets, chk
