#!/usr/bin/env python3
"""
JWTech: промежуточный YML-фид для tomas.kz на основе фида AL-Style.

Что делает:
  * цена = дилерская (purchase_price) + MARKUP, округление вверх до ROUND_TO тг;
  * если у поставщика цена 0 (розничная или дилерская) — товар «под заказ» без цены (type="on.demand");
  * убирает закупочную цену из фида;
  * убирает уценённые позиции (id с буквой на конце или параметр «Уценка=Да»);
  * наличие переносит в атрибут available, как требует tomas.kz;
  * чистит характеристики: служебные поля и дубли.

Защита: если исходный фид не скачался или в нём подозрительно мало товаров,
скрипт падает с ошибкой — старый опубликованный фид остаётся на месте,
и tomas.kz не уберёт товары в черновики из-за сбоя у поставщика.

Запуск:  python build_feed.py --src <URL или путь> --out public/feed.xml
"""
import argparse
import json
import math
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

from lxml import etree

MARKUP = float(os.environ.get("MARKUP", "0.10"))       # +10% к дилерской цене
ROUND_TO = int(os.environ.get("ROUND_TO", "10"))       # округление вверх до 10 тг
MIN_OFFERS = int(os.environ.get("MIN_OFFERS", "10000"))  # меньше — считаем фид битым

SHOP_NAME = "JWTech"
SHOP_URL = "https://jwtech.tomas.kz"

# Характеристики, которые покупателю не нужны или дублируют другие поля
DROP_PARAMS = {
    "артикул", "штрихкод", "снижена цена", "уценка", "причина уценки",
    "код нкт", "код тн вэд", "базовая единица", "в упаковке", "новинка", "код товара kaspi",
}

ALMATY = timezone(timedelta(hours=5))


def fetch(src: str) -> bytes:
    if re.match(r"^https?://", src):
        req = urllib.request.Request(src, headers={"User-Agent": "jwtech-feed/1.0"})
        with urllib.request.urlopen(req, timeout=300) as r:
            return r.read()
    with open(src, "rb") as f:
        return f.read()


def num(text):
    if text is None:
        return None
    t = re.sub(r"[^\d.,]", "", text).replace(",", ".")
    try:
        return float(t) if t else None
    except ValueError:
        return None


def clean_param_value(v: str) -> str:
    v = (v or "").strip()
    v = re.sub(r"<sup>\s*3\s*</sup>", "³", v)
    v = re.sub(r"<sup>\s*2\s*</sup>", "²", v)
    v = re.sub(r"<[^>]+>", "", v)
    return v


def is_markdown(offer) -> bool:
    oid = offer.get("id", "")
    if re.search(r"[^\d]$", oid):
        return True
    for p in offer.findall("param"):
        name = (p.get("name") or "").strip().lower()
        if name == "уценка" and (p.text or "").strip().lower() in ("да", "1", "true"):
            return True
    return False


def transform(raw: bytes):
    parser = etree.XMLParser(huge_tree=True, resolve_entities=False, no_network=True)
    root = etree.fromstring(raw, parser)
    shop = root.find("shop")
    if shop is None:
        raise SystemExit("В фиде нет блока <shop>")
    offers_el = shop.find("offers")
    src_offers = offers_el.findall("offer") if offers_el is not None else []
    if len(src_offers) < MIN_OFFERS:
        raise SystemExit(f"В исходном фиде всего {len(src_offers)} товаров (< {MIN_OFFERS}). Публикацию пропускаем.")

    stats = {"source_offers": len(src_offers), "markdown_removed": 0, "on_demand": 0,
             "in_stock": 0, "out_of_stock": 0, "published": 0}

    # Шапка магазина
    for tag, val in (("name", SHOP_NAME), ("company", SHOP_NAME), ("url", SHOP_URL)):
        el = shop.find(tag)
        if el is not None:
            el.text = val
    platform = shop.find("platform")
    if platform is not None:
        shop.remove(platform)

    for offer in src_offers:
        if is_markdown(offer):
            offers_el.remove(offer)
            stats["markdown_removed"] += 1
            continue

        pp_el = offer.find("purchase_price")
        dealer = num(pp_el.text) if pp_el is not None else None
        if pp_el is not None:
            offer.remove(pp_el)

        price_el = offer.find("price")
        retail = num(price_el.text) if price_el is not None else None
        avail_el = offer.find("available")
        avail = (avail_el.text or "").strip().lower() == "true" if avail_el is not None else False
        if avail_el is not None:
            offer.remove(avail_el)
        qty_el = offer.find("quantity")
        if qty_el is not None:
            offer.remove(qty_el)
        qis_el = offer.find("quantity_in_stock")
        qis = num(qis_el.text) if qis_el is not None else None

        vendor_el = offer.find("vendor")
        if vendor_el is not None and vendor_el.text:
            vendor_el.text = vendor_el.text.strip()

        # служебные ссылки на сайт поставщика покупателю не нужны
        url_el = offer.find("url")
        if url_el is not None:
            offer.remove(url_el)

        if not dealer or dealer <= 0 or not retail or retail <= 0:
            # Цена у поставщика не указана — «под заказ», без цены
            if price_el is not None:
                offer.remove(price_el)
            if qis_el is not None:
                offer.remove(qis_el)
            if "available" in offer.attrib:
                del offer.attrib["available"]
            offer.set("type", "on.demand")
            stats["on_demand"] += 1
        else:
            new_price = math.ceil(dealer * (1 + MARKUP) / ROUND_TO) * ROUND_TO
            if price_el is None:
                price_el = etree.SubElement(offer, "price")
            price_el.text = str(int(new_price))
            offer.set("available", "true" if avail else "false")
            if qis_el is not None:
                qis_el.text = str(int(qis)) if (avail and qis and qis > 0) else "0"
            if avail:
                stats["in_stock"] += 1
            else:
                stats["out_of_stock"] += 1

        # Характеристики: убираем служебные, дубли по названию (оставляем последнее значение — оно с единицами)
        params = offer.findall("param")
        last_by_name = {}
        for p in params:
            name = (p.get("name") or "").strip()
            if not name or name.lower() in DROP_PARAMS:
                offer.remove(p)
                continue
            p.text = clean_param_value(p.text)
            if not p.text:
                offer.remove(p)
                continue
            key = name.lower()
            if key in last_by_name:
                offer.remove(last_by_name[key])
            last_by_name[key] = p

        stats["published"] += 1

    root.set("date", datetime.now(ALMATY).strftime("%Y-%m-%d %H:%M"))
    stats["generated_at"] = root.get("date")
    stats["markup"] = MARKUP
    stats["round_to"] = ROUND_TO
    return root, stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=os.environ.get("FEED_URL"))
    ap.add_argument("--out", default="public/feed.xml")
    args = ap.parse_args()
    if not args.src:
        raise SystemExit("Не задан источник фида (--src или переменная FEED_URL)")

    raw = fetch(args.src)
    root, stats = transform(raw)

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    tree = etree.ElementTree(root)
    tree.write(args.out, xml_declaration=True, encoding="UTF-8", pretty_print=False)
    with open(os.path.join(os.path.dirname(args.out) or ".", "stats.json"), "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
