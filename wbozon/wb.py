"""Wildberries: продажи, заказы, остатки, отчёт о реализации, цены.

Используемые методы (сверено с документацией dev.wildberries.ru, октябрь 2026):
  statistics-api      GET  /api/v1/supplier/orders                        заказы (хранятся 90 дней)
  statistics-api      GET  /api/v1/supplier/sales                         продажи и возвраты (90 дней)
  seller-analytics    POST /api/analytics/v1/stocks-report/wb-warehouses   остатки на складах WB
  seller-analytics    POST /api/analytics/v1/stocks-report/seller-warehouses остатки FBS
  finance-api         POST /api/finance/v1/sales-reports/detailed          детализация отчёта о реализации
  discounts-prices    GET  /api/v2/list/goods/filter                       цены и скидки по размерам
"""
import datetime as dt

import pandas as pd

from .common import (RAW, START_DATE, ApiError, env, load_json, log, read_csv,
                     request, save_json, write_csv, yesterday)
from .normalize import wb_model

CABINET = "Агапова"
STAT = "https://statistics-api.wildberries.ru"
ANALYTICS = "https://seller-analytics-api.wildberries.ru"
FINANCE = "https://finance-api.wildberries.ru"
PRICES = "https://discounts-prices-api.wildberries.ru"

# Сколько последних дней перезагружать при каждом запуске (статусы меняются задним числом)
REFRESH_DAYS = 30


def _headers():
    token = env("WB_TOKEN_AGAPOVA")
    if not token:
        raise ApiError("WB_TOKEN_AGAPOVA не задан в .env")
    return {"Authorization": token}


def _num(x):
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


# ---------- Статистика: заказы и продажи ----------

def _fetch_stat(kind, date_from):
    """Выгрузка /supplier/{orders|sales} с пагинацией по lastChangeDate. Лимит 1 запрос в минуту."""
    rows, cursor, seen = [], date_from.isoformat(), set()
    while True:
        part = request("GET", f"{STAT}/api/v1/supplier/{kind}", headers=_headers(),
                       params={"dateFrom": cursor, "flag": 0}, pause=61)
        if not part:
            break
        new = [r for r in part if (r.get("srid"), r.get("saleID"), r.get("lastChangeDate")) not in seen]
        if not new:
            break
        for r in new:
            seen.add((r.get("srid"), r.get("saleID"), r.get("lastChangeDate")))
        rows.extend(new)
        log.info("WB %s: получено %d строк (всего %d)", kind, len(part), len(rows))
        if len(part) < 80000:
            break
        cursor = part[-1]["lastChangeDate"]
    return rows


def _stat_frame(rows, kind):
    out = []
    for r in rows:
        date = r["date"][:10]
        if kind == "sales":
            is_return = str(r.get("saleID", "")).startswith("R") or _num(r.get("forPay")) < 0
            status = "return" if is_return else "sale"
            key = r.get("saleID", "")
        else:
            status = "cancelled" if r.get("isCancel") else "ordered"
            key = r.get("srid", "")
        pre = abs(_num(r.get("priceWithDisc")))
        out.append({
            "platform": "WB", "cabinet": CABINET, "date": date,
            "model": wb_model(r.get("supplierArticle", "")),
            "article": r.get("supplierArticle", ""), "size": r.get("techSize", ""),
            "qty": 1, "price_pre_spp": round(pre, 2),
            "price_after_spp": round(abs(_num(r.get("finishedPrice"))), 2),
            "revenue": round(pre, 2), "status": status,
            "for_pay": round(abs(_num(r.get("forPay"))), 2) if kind == "sales" else "",
            "spp": r.get("spp", ""),
            "barcode": r.get("barcode", ""), "nmId": r.get("nmId", ""),
            "warehouse": r.get("warehouseName", ""), "warehouse_type": r.get("warehouseType", ""),
            "brand": r.get("brand", ""), "subject": r.get("subject", ""),
            "srid": r.get("srid", ""), "doc_id": key, "source": "statistics",
            "cancel_date": (r.get("cancelDate") or "")[:10] if kind == "orders" else "",
        })
    return pd.DataFrame(out)


def _merge(path, new, cutoff):
    """Оставляем старые строки с датой до cutoff, остальное заменяем свежей выгрузкой."""
    old = read_csv(path)
    new = new.astype(str) if len(new) else new
    if old is not None and len(old):
        # строки из отчёта о реализации (история старше 90 дней) не трогаем
        keep = old[(old["date"] < cutoff.isoformat()) | (old.get("source", "") == "realization")]
        df = pd.concat([keep, new], ignore_index=True)
    else:
        df = new
    if len(df):
        df["model"] = df["article"].map(wb_model)  # пересчёт по всей истории — учитывает новый mapping.csv
        df = df.drop_duplicates(subset=["doc_id", "status"], keep="last")
        df = df[df["date"] <= yesterday().isoformat()]
        df = df.sort_values(["date", "article", "size"], kind="stable")
    write_csv(df, path)
    return df


def _cutoff(path):
    old = read_csv(path)
    if old is None or not len(old):
        return START_DATE
    last = dt.date.fromisoformat(old["date"].max())
    return max(START_DATE, last - dt.timedelta(days=REFRESH_DAYS))


def load_orders(data_dir):
    path = data_dir / "wb_orders.csv"
    cutoff = _cutoff(path)
    df = _stat_frame(_fetch_stat("orders", cutoff), "orders")
    if len(df):
        df = df[df["date"] >= cutoff.isoformat()]
    return _merge(path, df, cutoff)


def load_sales(data_dir, realization):
    """Продажи и возвраты. Статистика хранит 90 дней, более раннюю историю
    (с 16.10.2025) добираем из детализации отчёта о реализации."""
    path = data_dir / "wb_sales.csv"
    cutoff = _cutoff(path)
    df = _stat_frame(_fetch_stat("sales", cutoff), "sales")
    if len(df):
        df = df[df["date"] >= cutoff.isoformat()]
    merged = _merge(path, df, cutoff)

    stat = merged[merged["source"] == "statistics"] if len(merged) else merged
    stat_start = stat["date"].min() if len(stat) else yesterday().isoformat()
    have_real = len(merged) and (merged["source"] == "realization").any()
    if realization is not None and len(realization) and not have_real and stat_start > START_DATE.isoformat():
        back = _realization_as_sales(realization, stat_start)
        log.info("WB продажи: из отчёта о реализации добавлено %d строк за %s — %s",
                 len(back), START_DATE, stat_start)
        merged = pd.concat([back.astype(str), merged], ignore_index=True)
        merged = merged.sort_values(["date", "article", "size"], kind="stable")
        write_csv(merged, path)
    return merged


def _realization_as_sales(real, before):
    r = real[real["doc_type"].isin(["Продажа", "Возврат"]) & (real["quantity"].astype(float) > 0)].copy()
    r["date"] = r["sale_dt"].str[:10]
    r = r[(r["date"] >= START_DATE.isoformat()) & (r["date"] < before)]
    rows = []
    for _, x in r.iterrows():
        q = float(x["quantity"])
        pre = abs(float(x["retail_price_with_disc"] or 0))
        rows.append({
            "platform": "WB", "cabinet": CABINET, "date": x["date"],
            "model": wb_model(x["vendor_code"]), "article": x["vendor_code"], "size": x["size"],
            "qty": int(q), "price_pre_spp": round(pre, 2),
            "price_after_spp": round(abs(float(x["retail_amount"] or 0)) / q, 2),
            "revenue": round(pre * q, 2),
            "status": "return" if x["doc_type"] == "Возврат" else "sale",
            "for_pay": round(abs(float(x["for_pay"] or 0)), 2), "spp": x["spp"],
            "barcode": x["barcode"], "nmId": x["nmId"], "warehouse": x["warehouse"],
            "warehouse_type": "", "brand": x["brand"], "subject": x["subject"],
            "srid": x["srid"], "doc_id": "rr" + x["rrd_id"], "source": "realization", "cancel_date": "",
        })
    return pd.DataFrame(rows)


# ---------- Отчёт о реализации ----------

REAL_COLUMNS = {
    "reportId": "report_id", "dateFrom": "report_from", "dateTo": "report_to", "rrdId": "rrd_id",
    "rrDate": "rr_date", "orderDt": "order_dt", "saleDt": "sale_dt", "docTypeName": "doc_type",
    "sellerOperName": "operation", "bonusTypeName": "bonus_type", "nmId": "nmId",
    "vendorCode": "vendor_code", "techSize": "size", "sku": "barcode", "brandName": "brand",
    "subjectName": "subject", "officeName": "warehouse", "quantity": "quantity",
    "retailPrice": "retail_price", "retailPriceWithDisc": "retail_price_with_disc",
    "retailAmount": "retail_amount", "salePercent": "sale_percent", "spp": "spp",
    "commissionPercent": "commission_percent", "ppvzSalesCommission": "sales_commission",
    "ppvzReward": "pvz_reward", "acquiringFee": "acquiring_fee", "vw": "wb_reward",
    "vwNds": "wb_reward_vat", "forPay": "for_pay", "deliveryAmount": "delivery_count",
    "returnAmount": "return_count", "deliveryService": "logistics",
    "rebillLogisticCost": "rebill_logistics", "paidStorage": "storage", "penalty": "penalty",
    "deduction": "deduction", "paidAcceptance": "acceptance", "additionalPayment": "additional_payment",
    "cashbackAmount": "cashback_amount", "cashbackDiscount": "cashback_discount",
    "cashbackCommissionChange": "cashback_commission_change", "deliveryMethod": "delivery_method",
    "srid": "srid",
}


def load_realization():
    """Детализация отчётов о реализации (еженедельные отчёты, как в ЛК).
    Кэш в data/raw/wb_realization.csv, при каждом запуске перезапрашиваем последние 5 недель."""
    path = RAW / "wb_realization.csv"
    old = read_csv(path)
    if old is not None and len(old):
        date_from = dt.date.fromisoformat(old["rr_date"].max()) - dt.timedelta(days=35)
    else:
        # недели в отчётах — с понедельника, берём с запасом
        date_from = START_DATE - dt.timedelta(days=7)
    pages, rrd, total = [], 0, 0
    while True:
        body = {"dateFrom": date_from.isoformat(), "dateTo": yesterday().isoformat(),
                "limit": 100000, "rrdId": rrd, "period": "weekly"}
        part = request("POST", f"{FINANCE}/api/finance/v1/sales-reports/detailed",
                       headers=_headers(), body=body, pause=61)
        if not part:
            break
        # сразу оставляем только нужные колонки — полный ответ занимает слишком много памяти
        pages.append(pd.DataFrame([{v: r.get(k, "") for k, v in REAL_COLUMNS.items()} for r in part]).astype(str))
        total += len(part)
        rrd = part[-1]["rrdId"]
        log.info("WB реализация: +%d строк (всего %d), дата последней строки %s",
                 len(part), total, part[-1].get("rrDate"))
        if len(part) < 100000:
            break
        del part
    new = pd.concat(pages, ignore_index=True) if pages else pd.DataFrame(columns=list(REAL_COLUMNS.values()))
    if old is not None and len(old):
        df = pd.concat([old, new], ignore_index=True).drop_duplicates("rrd_id", keep="last")
    else:
        df = new
    df = df.sort_values(["rr_date", "rrd_id"], kind="stable")
    write_csv(df, path)
    return df


# ---------- Цены (справочник) ----------

def load_prices():
    rows, offset = [], 0
    while True:
        part = request("GET", f"{PRICES}/api/v2/list/goods/filter", headers=_headers(),
                       params={"limit": 1000, "offset": offset}, pause=1)
        goods = (part or {}).get("data", {}).get("listGoods") or []
        if not goods:
            break
        rows.extend(goods)
        offset += len(goods)
        if len(goods) < 1000:
            break
    save_json(rows, RAW / "wb_prices.json")
    return rows


def size_index(prices):
    """chrtId -> (nmId, vendorCode, techSize)"""
    idx = {}
    for g in prices:
        for s in g.get("sizes", []):
            idx[int(s["sizeID"])] = (g["nmID"], g["vendorCode"], s.get("techSizeName", ""))
    return idx


def barcode_index(*frames):
    """(nmId, size) -> {barcode, brand, subject, category, spp} из заказов/продаж/реализации."""
    idx = {}
    for f in frames:
        if f is None or not len(f):
            continue
        for _, r in f.iterrows():
            k = (str(r.get("nmId", "")), str(r.get("size", "")))
            cur = idx.setdefault(k, {})
            for col in ("barcode", "brand", "subject", "spp"):
                v = str(r.get(col, "") or "")
                if v and v != "0":
                    cur[col] = v
    return idx


# ---------- Остатки ----------

def load_stocks(data_dir, prices, bidx):
    sidx = size_index(prices)
    out = []
    for kind, url in (("WB", "wb-warehouses"), ("FBS", "seller-warehouses")):
        offset = 0
        while True:
            part = request("POST", f"{ANALYTICS}/api/analytics/v1/stocks-report/{url}",
                           headers=_headers(), body={"limit": 250000, "offset": offset}, pause=21)
            items = (part or {}).get("data", {}).get("items") or []
            for it in items:
                nm, vendor, size = sidx.get(int(it["chrtId"]), (it["nmId"], "", ""))
                info = bidx.get((str(it["nmId"]), size), {})
                out.append({
                    "date": dt.date.today().isoformat(), "platform": "WB", "cabinet": CABINET,
                    "model": wb_model(vendor), "article": vendor, "size": size,
                    "barcode": info.get("barcode", ""), "nmId": it["nmId"], "chrtId": it["chrtId"],
                    "warehouse_type": kind, "warehouse": it.get("warehouseName", ""),
                    "region": it.get("regionName", ""), "stock": it.get("quantity", 0),
                    "in_way_to_client": it.get("inWayToClient", 0 if kind == "WB" else ""),
                    "in_way_from_client": it.get("inWayFromClient", 0 if kind == "WB" else ""),
                })
            if len(items) < 250000:
                break
            offset += len(items)
    df = pd.DataFrame(out)
    write_csv(df, data_dir / f"wb_stock_{dt.date.today().isoformat()}.csv")
    return df


# ---------- Справочник ----------

def catalog_rows(prices, bidx):
    rows = []
    for g in prices:
        for s in g.get("sizes", []):
            info = bidx.get((str(g["nmID"]), s.get("techSizeName", "")), {})
            if not info:  # для карточек без продаж берём бренд/предмет с любого размера
                info = next((v for (nm, _), v in bidx.items() if nm == str(g["nmID"])), {})
                info = {k: v for k, v in info.items() if k != "barcode"}
            rows.append({
                "platform": "WB", "cabinet": CABINET, "model": wb_model(g["vendorCode"]),
                "article": g["vendorCode"], "size": s.get("techSizeName", ""),
                "barcode": info.get("barcode", ""), "nmId_or_sku": g["nmID"],
                "price": s.get("price", ""), "discount": g.get("discount", ""),
                "price_with_discount": s.get("discountedPrice", ""),
                "spp": info.get("spp", ""), "category": info.get("subject", ""),
                "brand": info.get("brand", ""),
            })
    return rows
