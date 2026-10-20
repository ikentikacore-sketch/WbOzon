"""Компактные файлы для вставки в чат: data/for_chat/ и data/for_chat.zip.

Полные файлы большие (финансы WB — десятки МБ за период), поэтому здесь строки
сгруппированы по дате/периоду 16–15, площадке, модели, размеру и статусу.
"""
import shutil
import zipfile

import pandas as pd

from .common import DATA, log, period_of, read_csv

OUT = DATA / "for_chat"

README = """Выгрузка WB (кабинет «Агапова») и Озон (кабинет «JUST ME»).
Деньги в рублях, даты в формате YYYY-MM-DD. Период 16–15 обозначен месяцем окончания: 2026-10 = 16.09–15.10.2026.

summary.xlsx              сводка: продажи по модели и размеру, остатки, финансы
sales_by_period.csv       продажи и возвраты WB+Озон по периодам 16–15 (status: sale/delivered/return), шт и руб до СПП
sales_by_day.csv          то же по дням
orders_by_period.csv      заказы WB+Озон по периодам (status: ordered/cancelled/delivered/…). Заказы WB есть с 26.04.2026
orders_by_day.csv         то же по дням
stock_<дата>.csv          остатки на дату по модели и размеру
                          WB: stock, in_way_to_client, in_way_from_client (WB = склады WB, FBS = свой склад)
                          Озон: free_to_sell и reserved (FBO_TOTAL/FBS_TOTAL — все склады), FBO — по складам Озона с in_transit
finance_by_period.csv     финансы по периодам: WB — колонки как в ЛК; Озон — суммы начислений по типам, to_transfer = итог
catalog.csv               справочник: артикул, размер, баркод, nmId/SKU, цена, скидка, СПП, категория, бренд, model
check_<период>.csv        сверка за последний закрытый период
"""


def _num(df, cols):
    for c in cols:
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    return df


def _agg(names):
    frames = [read_csv(DATA / f"{n}.csv") for n in names]
    frames = [f for f in frames if f is not None and len(f)]
    if not frames:
        return None, None
    d = _num(pd.concat(frames, ignore_index=True), ["qty", "revenue"])
    keys = ["platform", "model", "size", "status"]
    day = d.groupby(["date"] + keys)[["qty", "revenue"]].sum().reset_index().round(2)
    per = d.assign(period=d["date"].map(period_of)).groupby(["period"] + keys)[["qty", "revenue"]].sum()
    return day, per.reset_index().round(2)


def _stock():
    def latest(prefix):
        files = sorted(DATA.glob(f"{prefix}_stock_*.csv"))
        return (files[-1].stem.rsplit("_", 1)[1], read_csv(files[-1])) if files else ("", None)
    dw, wb = latest("wb")
    do, oz = latest("ozon")
    day = max(dw, do)
    if not day:
        return None, None
    parts = []
    if wb is not None and len(wb):
        wb = _num(wb, ["stock", "in_way_to_client", "in_way_from_client"])
        parts.append(wb.groupby(["date", "platform", "model", "size", "warehouse_type"])
                     [["stock", "in_way_to_client", "in_way_from_client"]].sum().reset_index()
                     .rename(columns={"warehouse_type": "scheme"}))
    if oz is not None and len(oz):
        oz = _num(oz, ["free_to_sell", "reserved", "in_transit"])
        parts.append(oz.groupby(["date", "platform", "model", "size", "scheme"])
                     [["free_to_sell", "reserved", "in_transit"]].sum().reset_index())
    return day, pd.concat(parts, ignore_index=True) if parts else None


def build():
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    w = lambda df, name: df.to_csv(OUT / name, index=False, encoding="utf-8-sig")  # noqa: E731
    day, per = _agg(["wb_sales", "ozon_sales"])
    if day is not None:
        w(day, "sales_by_day.csv")
        w(per, "sales_by_period.csv")
    day, per = _agg(["wb_orders", "ozon_orders"])
    if day is not None:
        w(day, "orders_by_day.csv")
        w(per, "orders_by_period.csv")
    sday, st = _stock()
    if st is not None:
        w(st, f"stock_{sday}.csv")
    fin = read_csv(DATA / "raw" / "finance_totals.csv")
    if fin is not None:
        w(fin, "finance_by_period.csv")
    for name in ["catalog.csv", "summary.xlsx"] + [p.name for p in sorted(DATA.glob("check_*.csv"))[-1:]]:
        if (DATA / name).exists():
            shutil.copy(DATA / name, OUT / name)
    (OUT / "README.txt").write_text(README, encoding="utf-8")
    zpath = DATA / "for_chat.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(OUT.iterdir()):
            z.write(p, f"for_chat/{p.name}")
    log.info("Файлы для чата: %s (%d КБ)", zpath.relative_to(DATA.parent), zpath.stat().st_size // 1024)
    return zpath
