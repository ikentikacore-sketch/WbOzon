"""Озон, кабинет «JUST ME» (бренды JustMe и PerfectKids).

Используемые методы (api-seller.ozon.ru), сверено с объявлениями Ozon Seller API на октябрь 2026:
  POST /v3/posting/fbo/list                 отправления FBO (v2 отключён 31.08.2026)
  POST /v4/posting/fbs/list                 отправления FBS (v3 отключён 31.08.2026)
  POST /v1/returns/list                     возвраты FBO и FBS
  POST /v1/analytics/stocks                 остатки на складах Озона (по складам/кластерам)
  POST /v4/product/info/stocks              остатки FBO/FBS по товару (свободно и резерв)
  POST /v2/product/info/stocks-by-warehouse/fbs  остатки на своих складах FBS
  POST /v1/finance/accrual/by-day           начисления за день (вместо /v3/finance/transaction/list, отключён 08.09.2026)
  POST /v1/finance/accrual/types            справочник типов начислений
  POST /v3/product/list, /v3/product/info/list, /v4/product/info/attributes, /v5/product/info/prices,
       /v1/description-category/tree        справочник товаров и цен
"""
import datetime as dt

import pandas as pd

from .common import (RAW, START_DATE, ApiError, env, load_json, log, read_csv,
                     request, save_json, write_csv, yesterday)
from .normalize import ozon_model, split_ozon_offer

CABINET = "JUST ME"
API = "https://api-seller.ozon.ru"
REFRESH_DAYS = 45  # отправления меняют статус до доставки/возврата, перезагружаем последние 45 дней
MSK = dt.timezone(dt.timedelta(hours=3))


def _headers():
    cid, key = env("OZON_CLIENT_ID"), env("OZON_API_KEY")
    if not cid or not key:
        raise ApiError("OZON_CLIENT_ID или OZON_API_KEY не заданы в .env")
    return {"Client-Id": cid, "Api-Key": key, "Content-Type": "application/json"}


def post(path, body, pause=0.4):
    return request("POST", API + path, headers=_headers(), body=body, pause=pause) or {}


def _num(x):
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def _msk_date(ts):
    """Озон отдаёт время в UTC, площадка считает сутки по Москве."""
    if not ts:
        return ""
    try:
        t = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=dt.timezone.utc)
        return t.astimezone(MSK).date().isoformat()
    except ValueError:
        return str(ts)[:10]


def _result(resp):
    return resp.get("result", resp) if isinstance(resp, dict) else resp


# ---------- Справочник товаров ----------

def load_products():
    """offer_id -> {product_id, sku, barcodes, name, type_id, brand, price, ...}"""
    ids, last_id = [], ""
    while True:
        r = _result(post("/v3/product/list", {"filter": {"visibility": "ALL"}, "last_id": last_id, "limit": 1000}))
        items = r.get("items") or []
        ids.extend(i["product_id"] for i in items)
        last_id = r.get("last_id") or ""
        if len(items) < 1000 or not last_id:
            break
    info = {}
    for i in range(0, len(ids), 1000):
        r = post("/v3/product/info/list", {"product_id": ids[i:i + 1000]})
        for it in r.get("items") or _result(r).get("items") or []:
            info[it["offer_id"]] = it
    # бренд — из атрибутов (85 «Бренд», 31 «Бренд в одежде и обуви»)
    last_id = ""
    while ids:
        r = post("/v4/product/info/attributes", {"filter": {"visibility": "ALL"}, "limit": 1000, "last_id": last_id})
        res = r.get("result") or []
        for it in res:
            brand = ""
            for a in it.get("attributes", []):
                if a.get("id") in (85, 31) and a.get("values"):
                    brand = a["values"][0].get("value", "")
                    break
            if it.get("offer_id") in info:
                info[it["offer_id"]]["_brand"] = brand
        last_id = r.get("last_id") or ""
        if len(res) < 1000 or not last_id:
            break
    save_json(info, RAW / "ozon_products.json")
    return info


def load_prices():
    out, cursor = {}, ""
    while True:
        r = post("/v5/product/info/prices", {"filter": {"visibility": "ALL"}, "cursor": cursor, "limit": 1000})
        items = r.get("items") or []
        for it in items:
            out[it["offer_id"]] = it
        cursor = r.get("cursor") or ""
        if len(items) < 1000 or not cursor:
            break
    save_json(out, RAW / "ozon_prices.json")
    return out


def load_category_names():
    names = {}

    def walk(nodes):
        for n in nodes or []:
            if n.get("type_id"):
                names[n["type_id"]] = n.get("type_name", "")
            walk(n.get("children"))
    try:
        walk(post("/v1/description-category/tree", {"language": "DEFAULT"}).get("result"))
    except ApiError as e:
        log.error("Озон: дерево категорий не получено: %s", e)
    return names


def sku_of(item):
    if item.get("sku"):
        return item["sku"]
    for s in item.get("sources") or []:
        if s.get("sku"):
            return s["sku"]
    return ""


def catalog_rows(products, prices, cat_names):
    rows = []
    for offer, it in products.items():
        p = (prices.get(offer) or {}).get("price") or {}
        base, size = split_ozon_offer(offer)
        price = _num(p.get("price") or it.get("price"))
        old = _num(p.get("old_price") or it.get("old_price"))
        mkt = _num(p.get("marketing_seller_price") or p.get("marketing_price") or it.get("marketing_price"))
        rows.append({
            "platform": "Ozon", "cabinet": CABINET, "model": ozon_model(offer), "article": offer,
            "size": size, "barcode": ";".join(it.get("barcodes") or []),
            "nmId_or_sku": sku_of(it), "price": old or price,
            "discount": round((1 - price / old) * 100) if old and price else "",
            "price_with_discount": price,
            # аналог СПП на Озоне — скидка за счёт площадки: цена продавца vs цена для покупателя
            "spp": round((1 - mkt / price) * 100) if mkt and price and mkt < price else "",
            "category": cat_names.get(it.get("type_id"), ""), "brand": it.get("_brand", ""),
        })
    return rows


# ---------- Отправления (заказы и продажи) ----------

def _postings(path, since, to):
    out, cursor = [], ""
    while True:
        body = {"filter": {"since": since.strftime("%Y-%m-%dT00:00:00Z"),
                           "to": to.strftime("%Y-%m-%dT23:59:59Z")},
                "limit": 1000, "cursor": cursor,
                "with": {"analytics_data": True, "financial_data": True}}
        r = _result(post(path, body))
        part = r.get("postings") if isinstance(r, dict) else r
        part = part or []
        out.extend(part)
        cursor = r.get("cursor") if isinstance(r, dict) else ""
        if not part or not cursor or r.get("has_next") is False:
            break
    return out


def _status(s):
    s = (s or "").lower()
    if s == "delivered":
        return "delivered"
    if s == "cancelled":
        return "cancelled"
    return s


def _posting_rows(postings, scheme):
    rows = []
    for p in postings:
        fin = {}
        for fp in ((p.get("financial_data") or {}).get("products") or []):
            fin[fp.get("product_id")] = fp
        an = p.get("analytics_data") or {}
        for pr in p.get("products") or []:
            offer = pr.get("offer_id", "")
            qty = int(pr.get("quantity") or 1)
            price = _num(pr.get("price"))
            f = fin.get(pr.get("sku")) or {}
            client = _num(f.get("customer_price") or f.get("client_price")) or price
            base, size = split_ozon_offer(offer)
            rows.append({
                "platform": "Ozon", "cabinet": CABINET,
                "date": _msk_date(p.get("created_at") or p.get("in_process_at")),
                "model": ozon_model(offer), "article": offer, "size": size, "qty": qty,
                "price_pre_spp": round(price, 2), "price_after_spp": round(client, 2),
                "revenue": round(price * qty, 2), "status": _status(p.get("status")),
                "sku": pr.get("sku", ""), "scheme": scheme, "posting_number": p.get("posting_number", ""),
                "ozon_status": p.get("status", ""), "substatus": p.get("substatus", ""),
                "warehouse": an.get("warehouse_name") or (p.get("delivery_method") or {}).get("warehouse", ""),
                "delivery_date": _msk_date(p.get("fact_delivery_date") or ""),
                "old_price": pr.get("old_price", ""),
            })
    return rows


def _chunks(start, end, days=28):
    d = start
    while d <= end:
        e = min(end, d + dt.timedelta(days=days - 1))
        yield d, e
        d = e + dt.timedelta(days=1)


def load_orders_and_sales(data_dir):
    o_path, s_path = data_dir / "ozon_orders.csv", data_dir / "ozon_sales.csv"
    old = read_csv(o_path)
    if old is not None and len(old):
        since = max(START_DATE, dt.date.fromisoformat(old["date"].max()) - dt.timedelta(days=REFRESH_DAYS))
    else:
        since = START_DATE
    to = yesterday()
    rows, errors = [], []
    for path, scheme in (("/v3/posting/fbo/list", "FBO"), ("/v4/posting/fbs/list", "FBS")):
        try:
            for a, b in _chunks(since, to):
                rows += _posting_rows(_postings(path, a, b), scheme)
            log.info("Озон %s: строк %d", scheme, len([r for r in rows if r["scheme"] == scheme]))
        except ApiError as e:
            errors.append(f"{scheme}: {e}")
            log.error("Озон %s: %s", scheme, e)
    new = pd.DataFrame(rows)
    if errors and not len(new):
        raise ApiError("; ".join(errors))
    cols = ["platform", "cabinet", "date", "model", "article", "size", "qty", "price_pre_spp",
            "price_after_spp", "revenue", "status", "sku", "scheme", "posting_number", "ozon_status",
            "substatus", "warehouse", "delivery_date", "old_price"]
    new = new.reindex(columns=cols)
    new = new[(new["date"] >= since.isoformat()) & (new["date"] <= to.isoformat())]
    orders = _merge(o_path, new, since, ["posting_number", "sku"])

    # Продажи: доставленные отправления (дата доставки, если Озон её отдал) + возвраты отдельными строками
    sales = orders[orders["status"] == "delivered"].copy()
    if len(sales):
        sales["date"] = sales.apply(lambda r: r["delivery_date"] or r["date"], axis=1)
    try:
        ret = pd.DataFrame(_returns(START_DATE if old is None else since, to))
    except ApiError as e:
        log.error("Озон возвраты: %s", e)
        ret = pd.DataFrame()
    old_s = read_csv(s_path)
    if len(ret):
        ret = ret.reindex(columns=cols + ["return_id"])
        if old_s is not None and "return_id" in old_s:
            ret = pd.concat([old_s[old_s["status"] == "return"], ret.astype(str)]).drop_duplicates("return_id", keep="last")
    elif old_s is not None and len(old_s):
        ret = old_s[old_s["status"] == "return"]
    sales = pd.concat([sales.astype(str), ret.astype(str) if len(ret) else ret], ignore_index=True)
    if len(sales):
        sales["model"] = sales["article"].map(ozon_model)
        sales = sales[sales["date"] <= to.isoformat()].sort_values(["date", "article"], kind="stable")
    write_csv(sales, s_path)
    return orders, sales


def _merge(path, new, cutoff, keys):
    old = read_csv(path)
    new = new.astype(str)
    if old is not None and len(old):
        old = old[old["date"] < cutoff.isoformat()]
        df = pd.concat([old, new], ignore_index=True)
    else:
        df = new
    df["model"] = df["article"].map(ozon_model)  # пересчёт по всей истории — учитывает новый mapping.csv
    df = df.drop_duplicates(subset=keys, keep="last").sort_values(["date", "article"], kind="stable")
    write_csv(df, path)
    return df


def _returns(since, to):
    rows, last_id = [], 0
    while True:
        body = {"filter": {"logistic_return_date": {"time_from": since.strftime("%Y-%m-%dT00:00:00Z"),
                                                    "time_to": to.strftime("%Y-%m-%dT23:59:59Z")}},
                "limit": 500, "last_id": last_id}
        r = post("/v1/returns/list", body)
        items = r.get("returns") or []
        for it in items:
            pr = it.get("product") or {}
            offer = pr.get("offer_id", "")
            price = _num((pr.get("price") or {}).get("price") if isinstance(pr.get("price"), dict) else pr.get("price"))
            qty = int(pr.get("quantity") or 1)
            lg = it.get("logistic") or {}
            base, size = split_ozon_offer(offer)
            rows.append({
                "platform": "Ozon", "cabinet": CABINET,
                "date": _msk_date(lg.get("return_date") or lg.get("final_moment") or it.get("return_date")),
                "model": ozon_model(offer), "article": offer, "size": size, "qty": qty,
                "price_pre_spp": round(price, 2), "price_after_spp": round(price, 2),
                "revenue": round(price * qty, 2), "status": "return", "sku": pr.get("sku", ""),
                "scheme": it.get("schema", ""), "posting_number": it.get("posting_number", ""),
                "ozon_status": ((it.get("visual") or {}).get("status") or {}).get("sys_name", ""),
                "return_id": it.get("id", ""),
            })
        if not items or not r.get("has_next"):
            break
        last_id = items[-1].get("id", 0)
    return rows


# ---------- Остатки ----------

STOCK_RENAME = {
    "available_stock_count": "free_to_sell", "valid_stock_count": "valid",
    "transit_stock_count": "in_transit", "requested_stock_count": "requested",
    "return_from_customer_stock_count": "return_from_customer",
    "return_to_seller_stock_count": "return_to_seller", "reserved_stock_count": "reserved",
    "waiting_docs_stock_count": "waiting_docs", "expiring_stock_count": "expiring",
    "defect_stock_count": "defect", "stock_defect_stock_count": "defect",
}


def load_stocks(data_dir, products):
    day = dt.date.today().isoformat()
    rows = []
    skus = [s for s in (sku_of(p) for p in products.values()) if s]
    offer_by_sku = {str(sku_of(p)): o for o, p in products.items()}
    # 1) склады Озона с разбивкой по складам/кластерам
    for i in range(0, len(skus), 100):
        r = post("/v1/analytics/stocks", {"skus": [str(s) for s in skus[i:i + 100]]})
        for it in r.get("items") or []:
            offer = it.get("offer_id") or offer_by_sku.get(str(it.get("sku")), "")
            row = {"date": day, "platform": "Ozon", "cabinet": CABINET, "model": ozon_model(offer),
                   "article": offer, "size": split_ozon_offer(offer)[1], "sku": it.get("sku", ""),
                   "scheme": "FBO",
                   "warehouse": it.get("warehouse_name") or it.get("cluster_name", ""),
                   "cluster": it.get("cluster_name", "")}
            for k, v in it.items():
                if k.endswith("_count") or k.endswith("_stock"):
                    row[STOCK_RENAME.get(k, k)] = v
            rows.append(row)
    # 2) резерв: в /v1/analytics/stocks его нет, берём из /v4/product/info/stocks (по товару, все склады FBO/FBS)
    cursor = ""
    while True:
        r = post("/v4/product/info/stocks", {"filter": {"visibility": "ALL"}, "limit": 1000, "cursor": cursor})
        items = r.get("items") or []
        for it in items:
            offer = it.get("offer_id", "")
            for s in it.get("stocks") or []:
                rows.append({"date": day, "platform": "Ozon", "cabinet": CABINET, "model": ozon_model(offer),
                             "article": offer, "size": split_ozon_offer(offer)[1], "sku": s.get("sku", ""),
                             "scheme": (s.get("type") or "").upper() + "_TOTAL", "warehouse": "все склады",
                             "free_to_sell": max(0, int(s.get("present") or 0) - int(s.get("reserved") or 0)),
                             "present": s.get("present", 0), "reserved": s.get("reserved", 0)})
        cursor = r.get("cursor") or ""
        if len(items) < 1000 or not cursor:
            break
    # 3) свои склады FBS по складам
    for i in range(0, len(skus), 500):
        try:
            r = post("/v2/product/info/stocks-by-warehouse/fbs", {"sku": [str(s) for s in skus[i:i + 500]]})
        except ApiError as e:
            log.error("Озон остатки FBS по складам: %s", e)
            break
        for it in r.get("result") or r.get("items") or []:
            offer = it.get("offer_id", "")
            rows.append({"date": day, "platform": "Ozon", "cabinet": CABINET, "model": ozon_model(offer),
                         "article": offer, "size": split_ozon_offer(offer)[1], "sku": it.get("sku", ""),
                         "scheme": "FBS", "warehouse": it.get("warehouse_name", ""),
                         "free_to_sell": max(0, int(it.get("present") or 0) - int(it.get("reserved") or 0)),
                         "present": it.get("present", 0), "reserved": it.get("reserved", 0)})
    df = pd.DataFrame(rows)
    first = ["date", "platform", "cabinet", "model", "article", "size", "sku", "scheme", "warehouse",
             "free_to_sell", "reserved", "in_transit"]
    df = df.reindex(columns=first + [c for c in df.columns if c not in first])
    write_csv(df, data_dir / f"ozon_stock_{day}.csv")
    return df


# ---------- Финансы ----------

def load_accrual_types():
    path = RAW / "ozon_accrual_types.json"
    cached = load_json(path)
    if cached:
        return cached
    r = post("/v1/finance/accrual/types", {})
    types = {}

    def walk(o):
        if isinstance(o, dict):
            if "type_id" in o or "id" in o:
                tid = o.get("type_id", o.get("id"))
                name = o.get("name") or o.get("type_name") or o.get("title")
                if name:
                    types[str(tid)] = name
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(r)
    save_json(types, path)
    return types


def _accruals_day(day):
    """Сырые начисления за день, кэш в data/raw/ozon_accruals/. Закрытые дни (старше 14 дней) не перезапрашиваем."""
    path = RAW / "ozon_accruals" / f"{day}.json"
    if path.exists() and (dt.date.today() - day).days > 14:
        return load_json(path)
    out, last_id = [], ""
    while True:
        r = post("/v1/finance/accrual/by-day", {"date": day.isoformat(), "last_id": last_id})
        r = _result(r)
        out.extend(r.get("accruals") or [])
        last_id = r.get("last_id") or ""
        if not last_id or not r.get("accruals"):
            break
    save_json(out, path)
    return out


def _amount(o):
    if isinstance(o, dict):
        return _num(o.get("amount", o.get("value")))
    return _num(o)


def _leaf_fees(o, ctx, out):
    """Рекурсивно собирает услуги/комиссии: объекты с type_id и суммой."""
    if isinstance(o, dict):
        ctx = dict(ctx)
        for k in ("sku", "offer_id", "posting_number"):
            if o.get(k):
                ctx[k] = o[k]
        amt_key = next((k for k in ("accrued", "amount", "price") if k in o), None)
        if "type_id" in o and amt_key and not isinstance(o.get("fees"), list):
            out.append({**ctx, "type_id": o["type_id"], "amount": _amount(o[amt_key])})
        for k, v in o.items():
            if isinstance(v, (dict, list)):
                _leaf_fees(v, ctx, out)
    elif isinstance(o, list):
        for v in o:
            _leaf_fees(v, ctx, out)


def finance_rows(date_from, date_to):
    types = load_accrual_types()
    rows = []
    for d in _chunks(date_from, date_to, days=1):
        day = d[0]
        for a in _accruals_day(day):
            base = {"date": _msk_date(a.get("date")) or day.isoformat(), "accrual_id": a.get("accrual_id", ""),
                    "category": a.get("accrued_category", ""), "posting_number": a.get("unit_number", "")}
            rows.append({**base, "line": "total", "type_id": a.get("type_id", ""),
                         "type_name": types.get(str(a.get("type_id", "")), ""),
                         "sku": "", "offer_id": "", "amount": _amount(a.get("total_amount"))})
            fees = []
            _leaf_fees({k: v for k, v in a.items() if k not in ("total_amount",)}, {}, fees)
            for f in fees:
                rows.append({**base, "line": "fee", "type_id": f["type_id"],
                             "type_name": types.get(str(f["type_id"]), ""), "sku": f.get("sku", ""),
                             "offer_id": f.get("offer_id", ""), "amount": f["amount"]})
    df = pd.DataFrame(rows)
    if len(df):
        df.insert(0, "cabinet", CABINET)
        df.insert(0, "platform", "Ozon")
    return df
