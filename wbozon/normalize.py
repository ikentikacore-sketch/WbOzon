"""Приведение артикулов к единой модели (колонка model).

Озон: offer_id «БЖ11_42» -> модель «БЖ11», размер «42» (также «ШЖ1-Ч 128», «ШБ1-Син.146», «ТР11-42»).
WB:   артикул продавца — это уже артикул модели (размер хранится отдельно в techSize).
Затем модель прогоняется через mapping.csv и приводится к верхнему регистру, чтобы одинаковые модели с разным
написанием (ШКЮ1-Ч на Озоне и ШКЮ-1Ч на WB) получили один вид.

Формат mapping.csv (разделитель запятая или точка с запятой, первая строка — заголовок):
    platform,source,model
    Ozon,ШКЮ1-Ч,ШКЮ-1Ч
    WB,ШКЮ-1Ч,ШКЮ-1Ч
platform можно оставить пустым — тогда правило действует для обеих площадок.
"""
import csv
import re

from .common import ROOT, log

MAPPING_FILE = ROOT / "mapping.csv"
_mapping = None


def _key(s):
    return str(s or "").strip().upper().replace("Ё", "Е")


def load_mapping():
    global _mapping
    if _mapping is not None:
        return _mapping
    _mapping = {}
    if not MAPPING_FILE.exists():
        log.info("mapping.csv не найден — модели берутся без перекодировки")
        return _mapping
    text = MAPPING_FILE.read_text(encoding="utf-8-sig")
    dialect = csv.Sniffer().sniff(text.splitlines()[0], delimiters=",;\t")
    rows = list(csv.DictReader(text.splitlines(), dialect=dialect))
    for r in rows:
        r = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
        src, dst = r.get("source"), r.get("model")
        if not src or not dst:
            continue
        plat = r.get("platform", "").upper()
        _mapping[(plat, _key(src))] = dst
    log.info("mapping.csv: загружено %d правил", len(_mapping))
    return _mapping


def apply_mapping(model, platform):
    m = load_mapping()
    k = _key(model)
    return m.get((platform.upper(), k)) or m.get(("", k)) or model


# размер — число (или диапазон «134-140») в конце артикула после «_», пробела, точки или дефиса
_SIZE_RE = re.compile(r"^(.*?)[\s._-]+(\d{2,3}(?:-\d{2,3})?)$")


def split_ozon_offer(offer_id):
    """«БЖ11_42» -> («БЖ11», «42»); «ШЖ1-Ч 128» -> («ШЖ1-Ч», «128»); «ПЮ-1-134-140» -> («ПЮ-1», «134-140»).
    Число меньше 28 размером не считается (номер модели вида «-12»)."""
    offer_id = str(offer_id or "").strip()
    if "_" in offer_id:
        base, size = offer_id.rsplit("_", 1)
        return base.strip(), size.strip()
    m = _SIZE_RE.match(offer_id)
    if m and int(m.group(2).split("-")[0]) >= 28:
        return m.group(1).rstrip(" .-"), m.group(2)
    return offer_id, ""


def ozon_model(offer_id):
    return apply_mapping(split_ozon_offer(offer_id)[0], "OZON").upper()


def wb_model(vendor_code):
    return apply_mapping(str(vendor_code or "").strip(), "WB").upper()
