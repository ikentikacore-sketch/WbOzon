"""Ежедневная выгрузка WB и Озон в data/.

    python run.py              — полная выгрузка (её же запускает расписание в 06:00)
    python run.py --only wb    — только Wildberries
    python run.py --only ozon  — только Озон
    python run.py --head       — показать первые 10 строк каждого файла в data/
    python run.py --check      — сверка WB и Озон за последний закрытый период 16–15
    python run.py --pack       — собрать компактные файлы для чата в data/for_chat.zip
"""
import argparse
import sys
import time
import traceback

import pandas as pd

from wbozon import check, finance, ozon, pack, summary, wb
from wbozon.common import DATA, log, mask, read_csv, setup_logging, write_csv

errors = []


def step(name, fn, *args):
    t = time.time()
    log.info("=== %s ===", name)
    try:
        res = fn(*args)
        log.info("%s: готово за %.0f c", name, time.time() - t)
        return res
    except Exception as e:  # noqa: BLE001 — ошибка шага не останавливает выгрузку
        errors.append(f"{name}: {mask(e)}")
        log.error("%s: ОШИБКА %s\n%s", name, mask(e), mask(traceback.format_exc()))
        return None


def run_wb():
    prices = step("WB цены", wb.load_prices) or []
    real = step("WB отчёт о реализации", wb.load_realization)
    orders = step("WB заказы", wb.load_orders, DATA)
    sales = step("WB продажи", wb.load_sales, DATA, real)
    bidx = wb.barcode_index(orders, sales,
                            real.rename(columns={"barcode": "barcode"}) if real is not None else None)
    step("WB остатки", wb.load_stocks, DATA, prices, bidx)
    fin = step("WB финансы", finance.write_wb, DATA, real)
    return wb.catalog_rows(prices, bidx), fin


def run_ozon():
    products = step("Озон товары", ozon.load_products) or {}
    prices = step("Озон цены", ozon.load_prices) or {}
    cats = step("Озон категории", ozon.load_category_names) or {}
    res = step("Озон заказы и продажи", ozon.load_orders_and_sales, DATA)
    sales = res[1] if res else read_csv(DATA / "ozon_sales.csv")
    if products:
        step("Озон остатки", ozon.load_stocks, DATA, products)
    step("Озон финансы", finance.write_ozon, DATA, ozon)
    step("Озон цена после скидок", ozon.enrich_prices, DATA)
    fin = step("Озон итоги финансов", finance.ozon_totals, DATA)
    return ozon.catalog_rows(products, prices, cats), fin


def show_head():
    for p in sorted(DATA.glob("*.csv")):
        df = read_csv(p)
        print(f"\n===== {p.name}: {0 if df is None else len(df)} строк =====")
        if df is not None:
            with pd.option_context("display.max_columns", 30, "display.width", 250):
                print(df.head(10).to_string(index=False))
    x = DATA / "summary.xlsx"
    if x.exists():
        for name, df in pd.read_excel(x, sheet_name=None).items():
            print(f"\n===== summary.xlsx / {name}: {len(df)} строк =====")
            with pd.option_context("display.max_columns", 20, "display.width", 250):
                print(df.head(10).to_string(index=False))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["wb", "ozon"])
    ap.add_argument("--head", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--pack", action="store_true")
    a = ap.parse_args()
    setup_logging()
    if a.head:
        show_head()
        return 0
    if a.pack:
        print(pack.build())
        return 0
    if a.check:
        with pd.option_context("display.width", 250, "display.max_colwidth", 70):
            print(check.run().to_string(index=False))
        return 0
    log.info("##### Старт выгрузки #####")
    catalog, fins = [], []
    for name, fn in (("wb", run_wb), ("ozon", run_ozon)):
        if a.only and a.only != name:
            continue
        rows, fin = fn()
        catalog += rows
        if fin is not None and len(fin):
            fins.append(fin)

    if catalog:
        cat = pd.DataFrame(catalog)
        old = read_csv(DATA / "catalog.csv")
        if a.only and old is not None:  # при частичном запуске сохраняем строки другой площадки
            other = "Ozon" if a.only == "wb" else "WB"
            cat = pd.concat([old[old["platform"] == other], cat.astype(str)])
        write_csv(cat, DATA / "catalog.csv")
    fin_all = pd.concat(fins, ignore_index=True) if fins else None
    old_fin = read_csv(DATA / "raw" / "finance_totals.csv")
    if fin_all is not None and old_fin is not None:
        # итоги площадок, которые в этот раз не выгружались (или упали), берём из прошлого запуска
        done = set(fin_all["platform"])
        fin_all = pd.concat([old_fin[~old_fin["platform"].isin(done)], fin_all.astype(str)], ignore_index=True)
    if fin_all is not None:
        write_csv(fin_all, DATA / "raw" / "finance_totals.csv")
    else:
        fin_all = read_csv(DATA / "raw" / "finance_totals.csv")
    step("summary.xlsx", summary.build, DATA, fin_all)
    step("Файлы для чата", pack.build)

    if errors:
        log.warning("Выгрузка завершена с ошибками (%d):\n  %s", len(errors), "\n  ".join(errors))
        return 1
    log.info("##### Выгрузка завершена без ошибок #####")
    return 0


if __name__ == "__main__":
    sys.exit(main())
