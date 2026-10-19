"""Общие функции: настройки, лог, HTTP с повторами, запись файлов."""
import datetime as dt
import json
import logging
import os
import re
import time
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RAW = DATA / "raw"
LOG_FILE = ROOT / "log.txt"

# Начало истории по ТЗ
START_DATE = dt.date(2025, 10, 16)

# Единые колонки файлов продаж и заказов
SALES_COLUMNS = [
    "platform", "cabinet", "date", "model", "article", "size", "qty",
    "price_pre_spp", "price_after_spp", "revenue", "status",
]

load_dotenv(ROOT / ".env")

log = logging.getLogger("wbozon")


def setup_logging():
    DATA.mkdir(exist_ok=True)
    RAW.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    log.setLevel(logging.INFO)
    if not log.handlers:
        fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
        fh.setFormatter(fmt)
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        log.addHandler(fh)
        log.addHandler(sh)


def env(name, default=""):
    return os.getenv(name, default).strip()


def today():
    return dt.date.today()


def yesterday():
    return today() - dt.timedelta(days=1)


_SECRET_RE = re.compile(r"(eyJ[\w-]{10,}\.[\w-]+\.[\w-]+|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")


def mask(text):
    """Убирает из текста всё, похожее на токены, чтобы они не попали в лог."""
    return _SECRET_RE.sub("***", str(text))


class ApiError(Exception):
    pass


def request(method, url, *, headers, params=None, body=None, retries=6, pause=0.0, ok_empty=(204,)):
    """HTTP-запрос с повторами при 429/5xx/обрыве связи.

    Пауза при 429 берётся из заголовков X-Ratelimit-Retry / Retry-After, иначе растёт экспоненциально.
    Возвращает разобранный JSON или None при 204.
    """
    delay = 5
    for attempt in range(1, retries + 1):
        try:
            r = requests.request(method, url, headers=headers, params=params,
                                 json=body, timeout=180)
        except requests.RequestException as e:
            log.warning("Сетевая ошибка %s (попытка %d): %s", url, attempt, mask(e))
            time.sleep(delay)
            delay = min(delay * 2, 120)
            continue
        if r.status_code in ok_empty:
            time.sleep(pause)
            return None
        if r.status_code == 429 or r.status_code >= 500:
            wait = r.headers.get("X-Ratelimit-Retry") or r.headers.get("Retry-After")
            try:
                wait = float(wait) + 1
            except (TypeError, ValueError):
                wait = delay
                delay = min(delay * 2, 120)
            log.info("%s %s -> %d, жду %.0f c (попытка %d)", method, url, r.status_code, wait, attempt)
            time.sleep(wait)
            continue
        if r.status_code >= 400:
            raise ApiError(f"{method} {url} -> {r.status_code}: {mask(r.text[:500])}")
        time.sleep(pause)
        if not r.content:
            return None
        return r.json()
    raise ApiError(f"{method} {url}: не удалось после {retries} попыток")


def write_csv(df, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8-sig")
    log.info("Записан %s (%d строк)", path.relative_to(ROOT), len(df))


def read_csv(path):
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        return None
    return pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")


def save_json(obj, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def load_json(path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def merge_window(path, new_df, cutoff, date_col="date", key_cols=None):
    """Дозапись: старые строки до cutoff оставляем, с cutoff заменяем свежими.

    Площадки меняют статусы задним числом (отмены, возвраты, выкупы), поэтому
    последние дни каждый раз перезагружаются целиком.
    """
    old = read_csv(path)
    new_df = new_df.astype(str) if len(new_df) else new_df
    if old is not None and len(old):
        old = old[old[date_col] < cutoff.isoformat()]
        df = pd.concat([old, new_df], ignore_index=True)
    else:
        df = new_df
    if key_cols:
        df = df.drop_duplicates(subset=key_cols, keep="last")
    if len(df):
        df = df.sort_values(date_col, kind="stable")
    write_csv(df, path)
    return df


def period_of(date_str):
    """Период 16–15: дата 16.10–15.11 относится к периоду с меткой следующего месяца (2025-11)."""
    d = dt.date.fromisoformat(date_str[:10])
    if d.day >= 16:
        y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    else:
        y, m = d.year, d.month
    return f"{y:04d}-{m:02d}"


def period_bounds(label):
    y, m = map(int, label.split("-"))
    end = dt.date(y, m, 15)
    py, pm = (y - 1, 12) if m == 1 else (y, m - 1)
    return dt.date(py, pm, 16), end
