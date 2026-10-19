"""Финансовые отчёты по периодам 16–15 (метка периода — месяц, в котором он заканчивается:
2025-11 = 16.10.2025–15.11.2025)."""
import datetime as dt

import pandas as pd

from .common import START_DATE, env, log, period_bounds, period_of, write_csv, yesterday
from .normalize import ozon_model, wb_model

MONEY = ["retail_price", "retail_price_with_disc", "retail_amount", "sales_commission", "pvz_reward",
         "acquiring_fee", "wb_reward", "wb_reward_vat", "for_pay", "logistics", "rebill_logistics",
         "storage", "penalty", "deduction", "acceptance", "additional_payment", "cashback_amount",
         "cashback_discount", "cashback_commission_change"]


def tax_rate():
    try:
        return float(env("TAX_RATE") or 0)
    except ValueError:
        return 0.0


def periods():
    labels, d = [], START_DATE
    last = period_of(yesterday().isoformat())
    while True:
        p = period_of(d.isoformat())
        labels.append(p)
        if p == last:
            return labels
        d = period_bounds(p)[1] + dt.timedelta(days=1)


def wb_enrich(real):
    """Добавляет к строкам реализации знаковые суммы в терминах личного кабинета."""
    df = real.copy()
    for c in MONEY + ["quantity"]:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    sign = df["doc_type"].map({"Продажа": 1, "Возврат": -1}).fillna(0)
    df["sign"] = sign
    df["qty_sold"] = df["quantity"].where(sign > 0, 0)
    df["qty_returned"] = df["quantity"].where(sign < 0, 0)
    df["realization_pre_spp"] = sign * df["retail_price_with_disc"] * df["quantity"]
    df["sales"] = sign * df["retail_amount"]
    df["to_transfer"] = sign * df["for_pay"]
    df["acquiring"] = sign * df["acquiring_fee"]
    df["commission"] = df["sales"] - df["to_transfer"] - df["acquiring"]
    # «Вознаграждение Вайлдберриз (ВВ)» с НДС, как в ЛК; бывает отрицательным, когда WB компенсирует СПП
    df["wb_reward_total"] = sign * (df["wb_reward"] + df["wb_reward_vat"])
    df["tax"] = (df["sales"] * tax_rate()).round(2)
    df["model"] = df["vendor_code"].map(wb_model)
    df["period"] = df["rr_date"].map(period_of)
    return df


WB_TOTALS = ["qty_sold", "qty_returned", "realization_pre_spp", "sales", "to_transfer", "commission",
             "wb_reward_total", "acquiring", "logistics", "storage", "penalty", "deduction", "acceptance",
             "additional_payment", "tax"]


def write_wb(data_dir, real):
    if real is None or not len(real):
        return pd.DataFrame()
    df = wb_enrich(real)
    df = df[df["rr_date"] >= START_DATE.isoformat()]
    df.insert(0, "cabinet", "Агапова")
    df.insert(0, "platform", "WB")
    for p, part in df.groupby("period"):
        write_csv(part.drop(columns=["sign"]).round(2), data_dir / f"wb_finance_{p}.csv")
    tot = df.groupby("period")[WB_TOTALS].sum().round(2).reset_index()
    tot.insert(0, "platform", "WB")
    return tot


def write_ozon(data_dir, ozon_module, sales):
    frames = []
    for p in periods():
        a, b = period_bounds(p)
        b = min(b, yesterday())
        try:
            df = ozon_module.finance_rows(max(a, START_DATE), b)
        except Exception as e:  # noqa: BLE001 — ошибка одного периода не должна валить остальные
            log.error("Озон финансы %s: %s", p, e)
            continue
        if not len(df):
            continue
        df["period"] = p
        df["model"] = df["offer_id"].map(lambda x: ozon_model(x) if x else "")
        write_csv(df, data_dir / f"ozon_finance_{p}.csv")
        frames.append(df)
    if not frames:
        return pd.DataFrame()
    all_ = pd.concat(frames)
    tot = all_[all_["line"] == "total"].copy()
    tot["type_name"] = tot["type_name"].where(tot["type_name"] != "", "type_" + tot["type_id"].astype(str))
    piv = tot.pivot_table(index="period", columns="type_name", values="amount", aggfunc="sum").round(2)
    piv["to_transfer"] = piv.sum(axis=1)
    piv = piv.reset_index()
    if sales is not None and len(sales):
        s = sales.copy()
        s["period"] = s["date"].map(period_of)
        s["revenue"] = pd.to_numeric(s["revenue"], errors="coerce").fillna(0)
        s["qty"] = pd.to_numeric(s["qty"], errors="coerce").fillna(0)
        g = s.groupby(["period", "status"])[["qty", "revenue"]].sum().unstack(fill_value=0)
        add = pd.DataFrame({
            "qty_sold": g.get(("qty", "delivered"), 0), "qty_returned": g.get(("qty", "return"), 0),
            "realization_pre_spp": g.get(("revenue", "delivered"), 0) - g.get(("revenue", "return"), 0),
        }).reset_index()
        add["tax"] = (add["realization_pre_spp"] * tax_rate()).round(2)
        piv = add.merge(piv, on="period", how="outer")
    piv.insert(0, "platform", "Ozon")
    return piv
