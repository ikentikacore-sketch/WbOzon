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
                "limit": 100, "cursor": cursor,  # максимум 100 на страницу
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
            price = _amount(pr.get("price"))
            f = fin.get(pr.get("sku")) or {}
            client = _num(f.get("customer_price") or f.get("client_price")) or price  # Озон цену покупателя в отправлении не отдаёт
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
        ret = ret.reindex(columns=cols + ["return_id", "return_reason"])
        if old_s is not None and "return_id" in old_s:
            ret = pd.concat([old_s[old_s["status"] == "return"], ret.astype(str)]).drop_duplicates("return_id", keep="last")
    elif old_s is not None and len(old_s):
        ret = old_s[old_s["status"] == "return"]
    sales = pd.concat([sales.astype(str), ret.astype(str) if len(ret) else ret], ignore_index=True)
    if len(sales):
        sales["model"] = sales["article"].map(ozon_model)
        sales["size"] = sales["article"].map(lambda a: split_ozon_offer(a)[1])
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
    df["size"] = df["article"].map(lambda a: split_ozon_offer(a)[1])
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
            # Cancellation — возврат на склад после отмены/невыкупа: он уже есть в заказах как cancelled
            if it.get("type") == "Cancellation":
                continue
            pr = it.get("product") or {}
            offer = pr.get("offer_id", "")
            pp = pr.get("price")
            price = _num(pp.get("price", pp.get("amount"))) if isinstance(pp, dict) else _num(pp)
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
                "return_id": it.get("id", ""), "return_reason": it.get("return_reason_name", ""),
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
            r = post("/v2/product/info/stocks-by-warehouse/fbs", {"sku": [str(s) for s in skus[i:i + 500]], "limit": 1000})
        except ApiError as e:
            log.error("Озон остатки FBS по складам: %s", e)
            break
        for it in r.get("products") or r.get("result") or []:
            offer = it.get("offer_id", "")
            rows.append({"date": day, "platform": "Ozon", "cabinet": CABINET, "model": ozon_model(offer),
                         "article": offer, "size": split_ozon_offer(offer)[1], "sku": it.get("sku", ""),
                         "scheme": "FBS", "warehouse": it.get("warehouse_name", ""),
                         "free_to_sell": it.get("free_stock", max(0, int(it.get("present") or 0) - int(it.get("reserved") or 0))),
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
    types = {str(t["id"]): t.get("description") or t.get("name", "") for t in r.get("accrual_types") or []}
    save_json(types, path)
    return types


def _accruals_day(day):
    """Сырые начисления за день, кэш в data/raw/ozon_accruals/. Закрытые дни (старше 14 дней) не перезапрашиваем."""
    path = RAW / "ozon_accruals" / f"{day}.json"
    if path.exists() and (dt.date.today() - day).days > 14:
        return load_json(path)
    out, last_id = [], ""
    while True:
        r = _result(post("/v1/finance/accrual/by-day", {"date": day.isoformat(), "last_id": last_id}, pause=0.2))
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


def _components(a):
    """Раскладывает начисление на строки: выручка, комиссия, услуги с type_id.
    Сумма строк равна total_amount; если Озон добавит новое поле, разница уйдёт в строку «Прочее»."""
    out = []

    def fee(sku, qty, f):
        out.append({"sku": sku, "qty": qty, "type_id": f.get("type_id", ""), "amount": _amount(f.get("accrued"))})

    for pr in ((a.get("posting") or {}).get("products") or []):
        sku, qty = pr.get("sku", ""), pr.get("quantity", "")
        c = pr.get("commission") or {}
        if c:
            out.append({"sku": sku, "qty": qty, "type_id": "sale", "amount": _amount(c.get("sale_amount")),
                        "seller_price": _amount(c.get("seller_price")), "sale_price": _amount(c.get("sale_price"))})
            out.append({"sku": sku, "qty": qty, "type_id": "commission", "amount": _amount(c.get("commission"))})
        for f in ((pr.get("delivery") or {}).get("services") or []):
            fee(sku, qty, f)
        for f in (pr.get("services") or []):
            fee(sku, qty, f)
    for it in ((a.get("item_fees") or {}).get("fees") or []):
        for f in it.get("fees") or []:
            fee(it.get("sku", ""), it.get("quantity", ""), f)
    nf = a.get("non_item_fee")
    for f in (nf if isinstance(nf, list) else [nf] if nf else []):
        fee("", "", f)
    cf = a.get("container_fees")
    for f in (cf if isinstance(cf, list) else (cf or {}).get("fees") or ([cf] if cf else [])):
        if isinstance(f, dict) and "accrued" in f:
            fee("", "", f)
    diff = round(_amount(a.get("total_amount")) - sum(x["amount"] for x in out), 2)
    if abs(diff) >= 0.01:
        out.append({"sku": "", "qty": "", "type_id": "other", "amount": diff})
    return out


NAMES = {"sale": "Выручка (продажа по цене продавца)", "commission": "Комиссия за продажу", "other": "Прочее"}


def finance_rows(date_from, date_to):
    types = load_accrual_types()
    products = load_json(RAW / "ozon_products.json", {}) or {}
    offer_by_sku = {str(sku_of(p)): o for o, p in products.items()}
    rows = []
    for d in _chunks(date_from, date_to, days=1):
        day = d[0]
        for a in _accruals_day(day):
            base = {"date": _msk_date(a.get("date")) or day.isoformat(), "accrual_id": a.get("accrual_id", ""),
                    "category": a.get("accrued_category", ""), "posting_number": a.get("unit_number", ""),
                    "scheme": (a.get("posting") or {}).get("delivery_schema", "")}
            for c in _components(a):
                tid = str(c["type_id"])
                rows.append({**base, "type_id": tid, "type_name": NAMES.get(tid) or types.get(tid, f"тип {tid}"),
                             "sku": c["sku"], "offer_id": offer_by_sku.get(str(c["sku"]), ""), "qty": c["qty"],
                             "amount": round(c["amount"], 2), "seller_price": c.get("seller_price", ""),
                             "sale_price": c.get("sale_price", "")})
    df = pd.DataFrame(rows)
    if len(df):
        df.insert(0, "cabinet", CABINET)
        df.insert(0, "platform", "Ozon")
    return df


def enrich_prices(data_dir):
    """Из начислений (строки «Выручка», commission.sale_price) берём:
    - цену для покупателя после скидок за счёт Озона (аналог СПП) -> price_after_spp;
    - дату продажи (Озон начисляет выручку в момент доставки) -> date доставленных в ozon_sales.
    Ключ — отправление + SKU; строки возврата (отрицательная выручка) пропускаем."""
    price, sold = {}, {}
    for f in sorted(data_dir.glob("ozon_finance_*.csv")):
        df = read_csv(f)
        if df is None or not len(df):
            continue
        s = df[(df["type_id"] == "sale") & (pd.to_numeric(df["amount"], errors="coerce") > 0)]
        for _, r in s.iterrows():
            k = (r["posting_number"], str(r["sku"]))
            if r["sale_price"] != "":
                price[k] = round(abs(_num(r["sale_price"])) / (_num(r["qty"]) or 1), 2)
            sold.setdefault(k, r["date"])
    if not price and not sold:
        return 0
    n = 0
    for name in ("ozon_orders.csv", "ozon_sales.csv"):
        df = read_csv(data_dir / name)
        if df is None or not len(df):
            continue
        keys = list(zip(df["posting_number"], df["sku"].astype(str)))
        df["price_after_spp"] = df["price_pre_spp"]  # по умолчанию — цена продавца
        pr = pd.Series([price.get(k) for k in keys], index=df.index)
        df.loc[pr.notna(), "price_after_spp"] = pr[pr.notna()].astype(str)
        n += int(pr.notna().sum())
        if name == "ozon_sales.csv":
            d = pd.Series([sold.get(k) for k in keys], index=df.index)
            m = d.notna() & (df["status"] == "delivered")
            df.loc[m, "delivery_date"] = d[m]
            df.loc[m, "date"] = d[m]
            df = df[df["date"] <= yesterday().isoformat()].sort_values(["date", "article"], kind="stable")
        write_csv(df, data_dir / name)
    log.info("Озон: цена после скидок подставлена в %d строк, дата продажи — по начислениям", n)
    return n


def delivery_dates(postings):
    """Дата доставки покупателю (fact_delivery_date) по номерам отправлений: /v2/posting/fbo/get,
    для FBS — /v3/posting/fbs/get. В списках отправлений FBO этой даты нет.
    postings: [(posting_number, scheme)]. Кэш: data/raw/ozon_delivery_dates.json (дата по Москве)."""
    path = RAW / "ozon_delivery_dates.json"
    cache = load_json(path, {}) or {}
    todo = [(p, s) for p, s in postings if p not in cache]
    for i, (p, scheme) in enumerate(todo, 1):
        try:
            if scheme == "FBS":
                r = _result(post("/v3/posting/fbs/get", {"posting_number": p}, pause=0.05))
            else:
                r = _result(post("/v2/posting/fbo/get", {"posting_number": p, "with": {}}, pause=0.05))
            cache[p] = _msk_date(r.get("fact_delivery_date") or "")
        except ApiError as e:
            log.error("Озон дата доставки %s: %s", p, e)
        if i % 200 == 0:
            save_json(cache, path)
            log.info("Озон даты доставки: %d из %d", i, len(todo))
    save_json(cache, path)
    return cache
