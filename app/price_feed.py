"""
Prezzi live per ISIN → EUR, senza dipendenze esterne (solo stdlib urllib).

Fonti, in ordine:
  1. Tradegate (borsa tedesca dove opera Trade Republic): prezzo per ISIN
     già in EUR, robusto e senza rate-limit.  → primaria
  2. Yahoo Finance (ISIN→ticker→quote + cambio Frankfurter)  → riserva
  3. Cache su file (anche scaduta) se la rete fallisce  → la dashboard non si rompe
  4. Prezzo manuale impostato dall'utente (se presente)

TTL di cache configurabile; tutte le chiamate con timeout corto e try/except.
"""

import json
import os
import re
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone

CACHE_PATH  = os.path.join(os.path.dirname(__file__), "..", "data", "price_cache.json")
TTL_SECONDS = 15 * 60
TIMEOUT     = 8
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
EUR_SUFFIXES = (".DE", ".MI", ".AS", ".PA", ".F", ".SG", ".BE", ".VI", ".MC", ".XC")

_fx_cache = {}  # currency -> (rate, epoch)


def _http(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _http_json(url):
    return json.loads(_http(url))


def _parse_num(s):
    """Numero robusto: gestisce sia '122.845' (punto) sia '490,60' sia '1.234,56'."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    t = str(s).strip().replace("\xa0", "").replace(" ", "")
    if not t:
        return None
    comma, dot = t.rfind(","), t.rfind(".")
    if comma > dot:            # 1.234,56 → 1234.56
        t = t.replace(".", "").replace(",", ".")
    elif dot > comma:          # 1,234.56 → 1234.56
        t = t.replace(",", "")
    elif comma != -1:          # 490,60 → 490.60
        t = t.replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return None


def _load_cache():
    if os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_cache(cache):
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


# ── Fonte 1: Tradegate (EUR nativo) ───────────────────────────────────────────

def _tradegate_eur(isin):
    raw = _http(f"https://www.tradegate.de/refresh.php?isin={urllib.parse.quote(isin)}")
    d = json.loads(raw)
    last = _parse_num(d.get("last"))
    bid, ask = _parse_num(d.get("bid")), _parse_num(d.get("ask"))
    price = last if (last and last > 0) else (
        (bid + ask) / 2 if (bid and ask) else None)
    if price and price > 0:
        return round(price, 4), "tradegate"
    return None


# ── Fonte 2: Yahoo Finance (riserva) ──────────────────────────────────────────

def _fx_to_eur(currency):
    cur = (currency or "EUR").upper()
    if cur == "EUR":
        return 1.0
    now = time.time()
    if cur in _fx_cache and now - _fx_cache[cur][1] < TTL_SECONDS:
        return _fx_cache[cur][0]
    try:
        data = _http_json(f"https://api.frankfurter.app/latest?from={cur}&to=EUR")
        rate = float(data["rates"]["EUR"])
        _fx_cache[cur] = (rate, now)
        return rate
    except Exception:
        return None


def resolve_ticker(isin, cache):
    entry = cache.get(isin, {})
    if entry.get("ticker"):
        return entry["ticker"]
    q = urllib.parse.quote(isin)
    data = _http_json(
        f"https://query2.finance.yahoo.com/v1/finance/search?q={q}&quotesCount=15&newsCount=0")
    quotes = [qt for qt in data.get("quotes", []) if qt.get("symbol")]
    if not quotes:
        return None
    quotes.sort(key=lambda qt: (0 if any(qt["symbol"].endswith(s) for s in EUR_SUFFIXES) else 1,
                                len(qt["symbol"])))
    return quotes[0]["symbol"]


def _yahoo_eur(isin, cache):
    ticker = resolve_ticker(isin, cache)
    if not ticker:
        return None
    data = _http_json(
        f"https://query2.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(ticker)}"
        f"?range=1d&interval=1d")
    meta = data["chart"]["result"][0]["meta"]
    price = float(meta["regularMarketPrice"])
    fx = _fx_to_eur(meta.get("currency", "EUR"))
    if fx is None:
        return None
    cache.setdefault(isin, {})["ticker"] = ticker
    return round(price * fx, 4), "yahoo"


# ── API ───────────────────────────────────────────────────────────────────────

def get_eur_price(isin, force=False, cache=None):
    own = cache is None
    if cache is None:
        cache = _load_cache()
    entry = cache.get(isin, {})
    now = time.time()

    # prezzo manuale impostato dall'utente → ha priorità
    if entry.get("manual_eur_price") is not None:
        return {"isin": isin, "eur_price": entry["manual_eur_price"], "currency": "EUR",
                "ticker": entry.get("ticker"), "source": "manual", "ts": entry.get("ts")}

    # cache fresca
    if (not force and entry.get("eur_price") is not None
            and entry.get("ts_epoch") and now - entry["ts_epoch"] < TTL_SECONDS):
        return {"isin": isin, "eur_price": entry["eur_price"], "currency": entry.get("currency"),
                "ticker": entry.get("ticker"), "source": "cache", "ts": entry.get("ts")}

    # live: Tradegate poi Yahoo
    res = None
    for fetch in (lambda: _tradegate_eur(isin), lambda: _yahoo_eur(isin, cache)):
        try:
            res = fetch()
            if res:
                break
        except Exception:
            res = None
    if res:
        eur, provider = res
        # rileggi da cache: _yahoo_eur può averci scritto il ticker risolto
        entry = {**cache.get(isin, {}), "eur_price": eur, "currency": "EUR", "provider": provider,
                 "ts": datetime.now(timezone.utc).isoformat(), "ts_epoch": now}
        cache[isin] = entry
        if own:
            _save_cache(cache)
        return {"isin": isin, "eur_price": eur, "currency": "EUR",
                "ticker": entry.get("ticker"), "source": "live", "ts": entry["ts"]}

    # fallback: cache scaduta
    if entry.get("eur_price") is not None:
        return {"isin": isin, "eur_price": entry["eur_price"], "currency": entry.get("currency"),
                "ticker": entry.get("ticker"), "source": "cache_stale", "ts": entry.get("ts")}

    return {"isin": isin, "eur_price": None, "currency": None, "ticker": None,
            "source": "unavailable", "ts": None}


def get_prices(isins, force=False):
    cache = _load_cache()
    out = {isin: get_eur_price(isin, force=force, cache=cache) for isin in isins}
    _save_cache(cache)
    return out


def set_manual_price(isin, eur_price):
    cache = _load_cache()
    entry = cache.setdefault(isin, {})
    if eur_price is None:
        entry.pop("manual_eur_price", None)
    else:
        entry["manual_eur_price"] = round(float(eur_price), 4)
        entry["ts"] = datetime.now(timezone.utc).isoformat()
    _save_cache(cache)
    return entry
