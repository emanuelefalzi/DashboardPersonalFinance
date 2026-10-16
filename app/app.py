import os
import sys
import csv
import json
import io
import math
import shutil
import uuid
import calendar
import traceback
from datetime import datetime

import pandas as pd
from flask import Flask, request, jsonify, send_from_directory, send_file, abort

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"), override=True)

from file_parser import parse_file
from ai_categorizer import categorize_transactions
from investment_parser import parse_investment_pdf
from cost_basis import build_ledger
import price_feed
import metrics

app = Flask(
    __name__,
    template_folder=os.path.join(os.path.dirname(__file__), "..", "templates"),
    static_folder=os.path.join(os.path.dirname(__file__), "..", "static"),
)

# Hardening: limita la dimensione degli upload (protegge l'istanza always-on)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024   # 16 MB


@app.errorhandler(413)
def too_large(e):
    return jsonify({"error": "File troppo grande (max 16 MB)"}), 413


DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
CSV_PATH = os.path.join(DATA_DIR, "transactions.csv")
CATEGORIES_PATH = os.path.join(DATA_DIR, "categories.json")
ONBOARDING_PATH = os.path.join(DATA_DIR, "onboarding.json")
INVESTMENTS_PATH = os.path.join(DATA_DIR, "investments.json")
INVESTMENT_LOTS_PATH = os.path.join(DATA_DIR, "investment_lots.json")
PRICE_CACHE_PATH = os.path.join(DATA_DIR, "price_cache.json")
RIMBORSI_PENDING_PATH = os.path.join(DATA_DIR, "rimborsi_pending.json")

ONBOARDING_BANKS = ["Trade Republic", "Banca Generali", "PayPal", "Investimento Iniziale"]

CSV_COLUMNS = ["data", "causale", "descrizione", "importo", "categoria", "banca", "tipo", "transaction_id"]

DEFAULT_CATEGORIES = [
    "Pasto", "Caffè", "Merendine", "Alcol", "Spesa", "Mezzi", "Viaggi", "Personali",
    "Acquisti Online", "Investimento", "Paghetta", "Cash Movement",
]


def load_categories():
    if os.path.exists(CATEGORIES_PATH):
        with open(CATEGORIES_PATH, encoding="utf-8") as f:
            return json.load(f)
    return DEFAULT_CATEGORIES.copy()


def save_categories_file(cats):
    backup_data()
    with open(CATEGORIES_PATH, "w", encoding="utf-8") as f:
        json.dump(cats, f, ensure_ascii=False, indent=2)


def load_onboarding():
    if os.path.exists(ONBOARDING_PATH):
        with open(ONBOARDING_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {b: {"amount": 0, "date": ""} for b in ONBOARDING_BANKS}


def save_onboarding_file(data):
    backup_data()
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(ONBOARDING_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_investments():
    if os.path.exists(INVESTMENTS_PATH):
        with open(INVESTMENTS_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"entities": []}


def save_investments(data):
    backup_data()
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(INVESTMENTS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_investment_lots():
    """Dettaglio trade (acquisti/vendite titoli) per il calcolo del costo medio.
    {"lots": [...], "opening_positions": [...]}."""
    if os.path.exists(INVESTMENT_LOTS_PATH):
        with open(INVESTMENT_LOTS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("lots", [])
        data.setdefault("opening_positions", [])
        return data
    return {"lots": [], "opening_positions": []}


def save_investment_lots(data):
    backup_data()
    os.makedirs(DATA_DIR, exist_ok=True)
    data.setdefault("lots", [])
    data.setdefault("opening_positions", [])
    with open(INVESTMENT_LOTS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ── Backup automatico ─────────────────────────────────────────────────────────
BACKUPS_DIR = os.path.join(DATA_DIR, "backups")
_BACKUP_FILES = [CSV_PATH, CATEGORIES_PATH, ONBOARDING_PATH,
                 INVESTMENTS_PATH, INVESTMENT_LOTS_PATH, PRICE_CACHE_PATH,
                 RIMBORSI_PENDING_PATH]


def backup_data():
    """Copia i file dati in data/backups/<YYYY-MM-DD>/ prima della prima scrittura
    distruttiva del giorno. Best-effort: non blocca mai la scrittura.
    Idempotente: copia solo i file ancora mancanti nella cartella di oggi, così una
    copia parziale (errore a metà) viene RITENTATA alla scrittura successiva e un
    file dati creato più tardi nella giornata viene comunque salvato."""
    try:
        day = datetime.now().strftime("%Y-%m-%d")
        dest = os.path.join(BACKUPS_DIR, day)
        present = [p for p in _BACKUP_FILES if os.path.exists(p)]
        # "completo per oggi" = ogni file esistente ha già la sua copia. Non basta
        # che la cartella esista: una copia parziale o un file nuovo va ripreso.
        if os.path.isdir(dest) and all(
            os.path.exists(os.path.join(dest, os.path.basename(p))) for p in present
        ):
            return
        os.makedirs(dest, exist_ok=True)
        for p in present:
            dst = os.path.join(dest, os.path.basename(p))
            if not os.path.exists(dst):
                shutil.copy2(p, dst)
        _prune_backups(keep=30)
    except Exception as e:
        try:
            app.logger.warning("backup_data fallito: %s", e)
        except Exception:
            pass


def _prune_backups(keep=30):
    try:
        if not os.path.isdir(BACKUPS_DIR):
            return
        days = sorted(d for d in os.listdir(BACKUPS_DIR)
                      if os.path.isdir(os.path.join(BACKUPS_DIR, d)))
        for d in days[:-keep]:
            shutil.rmtree(os.path.join(BACKUPS_DIR, d), ignore_errors=True)
    except Exception:
        pass


def ensure_csv():
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(CSV_PATH):
        with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
            writer.writeheader()


def load_csv():
    ensure_csv()
    try:
        df = pd.read_csv(CSV_PATH, dtype=str)
        if df.empty:
            return []
        df = df.fillna("")   # i campi vuoti (es. transaction_id) come "" e non float nan
        df["importo"] = pd.to_numeric(df["importo"], errors="coerce").fillna(0)
        return df.to_dict(orient="records")
    except pd.errors.EmptyDataError:
        return []


def save_csv(records):
    backup_data()
    ensure_csv()
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for r in records:
            row = {k: r.get(k, "") for k in CSV_COLUMNS}
            writer.writerow(row)


def dedup_key(t):
    # Se c'è un transaction_id (CSV Trade Republic) usalo: è univoco e stabile,
    # così non si scartano per errore transazioni diverse con stesso giorno/importo
    # e si riconoscono i ri-caricamenti dello stesso export.
    tid = str(t.get("transaction_id", "") or "").strip()
    if tid and tid.lower() not in ("nan", "none"):
        return ("tid", tid)
    return (str(t.get("data", "")), str(t.get("descrizione", "")).strip(), str(round(float(t.get("importo", 0)), 2)))


@app.route("/")
def index():
    return send_from_directory(app.template_folder, "index.html")


@app.route("/api/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return jsonify({"error": "Nessun file ricevuto"}), 400

    f = request.files["file"]
    banca = request.form.get("banca", "Altro")
    filename = f.filename
    file_bytes = f.read()

    try:
        existing = load_csv()
        parsed, reconciliation = parse_file(file_bytes, filename, banca, existing)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"Errore parsing file: {str(e)}"}), 500

    if not parsed:
        return jsonify({"error": "Nessuna transazione trovata nel file"}), 400

    # Flag duplicates vs existing ledger
    existing_keys = {dedup_key(t) for t in existing}
    for t in parsed:
        t["_duplicate"] = t.get("_duplicate", False) or dedup_key(t) in existing_keys

    # AI categorization + description simplification
    try:
        parsed = categorize_transactions(parsed)
    except Exception as e:
        import traceback
        traceback.print_exc()
        # Continue without AI — better than crashing

    return jsonify({"transactions": parsed, "reconciliation": reconciliation})


@app.route("/api/save", methods=["POST"])
def save():
    data = request.json
    if not data or "transactions" not in data:
        return jsonify({"error": "Dati mancanti"}), 400

    new_txs = data["transactions"]
    existing = load_csv()
    existing_keys = {dedup_key(t) for t in existing}

    lots_store  = load_investment_lots()
    lots_by_tid = {l.get("transaction_id"): l
                   for l in lots_store.get("lots", []) if l.get("transaction_id")}
    lots_added   = 0
    lots_changed = False

    added = 0
    for t in new_txs:
        k = dedup_key(t)
        if k not in existing_keys:
            clean = {col: t.get(col, "") for col in CSV_COLUMNS}
            clean["importo"] = round(float(clean.get("importo") or 0), 2)
            existing.append(clean)
            existing_keys.add(k)
            added += 1
        # Persisti il dettaglio trade (lotto) per il costo medio — upsert per transaction_id
        lot = t.get("_lot")
        if lot and lot.get("transaction_id"):
            tid = lot["transaction_id"]
            if lots_by_tid.get(tid) != lot:        # nuovo o modificato
                lots_changed = True
                if tid not in lots_by_tid:
                    lots_added += 1
            lots_by_tid[tid] = lot

    save_csv(existing)
    if lots_changed:
        lots_store["lots"] = list(lots_by_tid.values())
        save_investment_lots(lots_store)
    return jsonify({"saved": added, "total": len(existing), "lots_saved": lots_added})


@app.route("/api/investment-lots", methods=["GET"])
def get_investment_lots():
    return jsonify(load_investment_lots())


@app.route("/api/investment-lots/opening", methods=["POST"])
def set_opening_positions():
    body = request.get_json() or {}
    positions = body.get("opening_positions", body.get("positions", []))
    store = load_investment_lots()
    store["opening_positions"] = positions
    save_investment_lots(store)
    return jsonify({"ok": True, "count": len(positions)})


def _infer_inv_type(name):
    return "ETF" if "ETF" in (name or "").upper() else "S"


def _snapshot_portfolio(month):
    """'Foto' del portafoglio: per ogni titolo posseduto scrive in investments.json
    il valore di mercato (quote × prezzo live) per il mese dato. I prezzi live
    servono SOLO qui, per scattare la foto. Ritorna {isin: valore}."""
    store = load_investment_lots()
    led   = build_ledger(store.get("lots", []), store.get("opening_positions", []))
    isins = [p["isin"] for p in led["positions"]]
    if not isins:
        return {}
    prices  = price_feed.get_prices(isins, force=True)
    inv     = load_investments()
    by_isin = {e.get("isin"): e for e in inv.get("entities", [])}
    captured = {}
    for p in led["positions"]:
        eur = (prices.get(p["isin"]) or {}).get("eur_price")
        if eur is None:
            continue
        value = round(p["shares"] * eur, 2)
        captured[p["isin"]] = value
        e = by_isin.get(p["isin"])
        if not e:
            e = {"isin": p["isin"], "name": p["name"], "type": _infer_inv_type(p["name"]), "values": {}}
            by_isin[p["isin"]] = e
        e.setdefault("values", {})[month] = value
    inv["entities"] = list(by_isin.values())
    save_investments(inv)
    return captured


def _auto_snapshot_month_end():
    """L'ultimo giorno del mese scatta la foto automaticamente, se non già fatta."""
    today = datetime.now()
    if today.day != calendar.monthrange(today.year, today.month)[1]:
        return
    month = today.strftime("%Y-%m")
    inv = load_investments()
    if not any(month in (e.get("values") or {}) for e in inv.get("entities", [])):
        try:
            _snapshot_portfolio(month)
        except Exception:
            pass


def _latest_snapshot(inv, isins):
    """(mese della foto più recente, {isin: valore a quel mese})."""
    months = set()
    for e in inv.get("entities", []):
        if e.get("isin") in isins:
            for m, v in (e.get("values") or {}).items():
                if float(v or 0) > 0:
                    months.add(m)
    if not months:
        return None, {}
    snap_month = max(months)
    by_isin = {e.get("isin"): e for e in inv.get("entities", [])}
    vals = {}
    for isin in isins:
        e = by_isin.get(isin)
        if not e:
            continue
        v = (e.get("values") or {}).get(snap_month)
        if v is None:   # fallback: ultimo valore disponibile di quel titolo
            its = {m: vv for m, vv in (e.get("values") or {}).items() if float(vv or 0) > 0}
            v = its[max(its)] if its else None
        if v is not None:
            vals[isin] = float(v)
    return snap_month, vals


@app.route("/api/investments/live", methods=["GET"])
def investments_live():
    """Posizioni: costo medio dai trade + valore alla FOTO mensile più recente (fisso)."""
    _auto_snapshot_month_end()
    store = load_investment_lots()
    led   = build_ledger(store.get("lots", []), store.get("opening_positions", []))
    isins = [p["isin"] for p in led["positions"]]
    inv   = load_investments()
    snap_month, vals = _latest_snapshot(inv, isins)
    first_dates = _first_lot_dates(store.get("lots", []))

    positions = []
    tot_cost_all = 0.0
    tot_cost_valued = tot_value = 0.0
    valued = 0
    for p in led["positions"]:
        value      = round(vals[p["isin"]], 2) if p["isin"] in vals else None
        unrealized = round(value - p["cost_basis"], 2) if value is not None else None
        unreal_pct = (round(unrealized / p["cost_basis"] * 100, 2)
                      if value is not None and p["cost_basis"] > 0 else None)
        price_eur  = round(value / p["shares"], 4) if (value is not None and p["shares"]) else None
        tot_cost_all += p["cost_basis"]
        if value is not None:
            tot_cost_valued += p["cost_basis"]
            tot_value       += value
            valued          += 1
        positions.append({
            **p,
            "price_eur":      price_eur,
            "value":          value,
            "unrealized":     unrealized,
            "unrealized_pct": unreal_pct,
            "price_source":   "snapshot",
            "snapshot_month": snap_month,
            "data_sottoscrizione": first_dates.get(p["isin"]),
        })

    tot_unreal = round(tot_value - tot_cost_valued, 2)
    tot_pct    = round(tot_unreal / tot_cost_valued * 100, 2) if tot_cost_valued > 0 else None
    realized_total = round(sum((s["realized"] or 0) for s in led["sales"]), 2)

    return jsonify({
        "positions": positions,
        "snapshot_month": snap_month,
        "totals": {
            "cost_basis":       round(tot_cost_all, 2),
            "value":            round(tot_value, 2),
            "unrealized":       tot_unreal,
            "unrealized_pct":   tot_pct,
            "realized_total":   realized_total,
            "positions_valued": valued,
            "positions_total":  len(led["positions"]),
        },
        "sales":    led["sales"],
        "warnings": led["warnings"],
    })


@app.route("/api/investments/snapshot", methods=["POST"])
def post_snapshot():
    """Scatta la foto del mese (default: mese corrente) usando i prezzi live."""
    body  = request.get_json(silent=True) or {}
    month = body.get("month") or datetime.now().strftime("%Y-%m")
    captured = _snapshot_portfolio(month)
    return jsonify({"ok": True, "month": month, "count": len(captured), "captured": captured})


@app.route("/api/transactions", methods=["GET"])
def get_transactions():
    month = request.args.get("month")
    categoria = request.args.get("categoria")
    banca = request.args.get("banca")
    tipo = request.args.get("tipo")
    q = (request.args.get("q") or "").strip().lower()

    # Stamp each record with its real CSV position before filtering
    records = [dict(r, _idx=i) for i, r in enumerate(load_csv())]

    if month:
        records = [r for r in records if str(r.get("data", "")).startswith(month)]
    if categoria:
        records = [r for r in records if r.get("categoria") == categoria]
    if banca:
        records = [r for r in records if r.get("banca") == banca]
    if tipo:
        records = [r for r in records if r.get("tipo") == tipo]
    if q:
        records = [r for r in records
                   if q in str(r.get("descrizione", "")).lower()
                   or q in str(r.get("causale", "")).lower()
                   or q in str(r.get("categoria", "")).lower()]

    records.sort(key=lambda r: str(r.get("data", "")), reverse=True)

    return jsonify({"transactions": records, "total": len(records)})


@app.route("/api/transactions/<int:idx>", methods=["PATCH"])
def patch_transaction(idx):
    data = request.json
    records = load_csv()
    if idx < 0 or idx >= len(records):
        return jsonify({"error": "Indice non valido"}), 404

    allowed = ["categoria", "causale", "descrizione", "tipo", "banca"]
    for key in allowed:
        if key in data:
            records[idx][key] = data[key]

    save_csv(records)
    return jsonify({"ok": True, "record": records[idx]})


@app.route("/api/transactions/<int:idx>", methods=["DELETE"])
def delete_transaction(idx):
    records = load_csv()
    if idx < 0 or idx >= len(records):
        return jsonify({"error": "Indice non valido"}), 404
    records.pop(idx)
    save_csv(records)
    return jsonify({"ok": True})


@app.route("/api/transactions/delete-bulk", methods=["POST"])
def delete_bulk():
    data = request.get_json()
    indices = sorted(set(int(i) for i in data.get("indices", [])), reverse=True)
    records = load_csv()
    deleted = 0
    for idx in indices:
        if 0 <= idx < len(records):
            records.pop(idx)
            deleted += 1
    save_csv(records)
    return jsonify({"ok": True, "deleted": deleted})


@app.route("/api/transactions/update-bulk", methods=["POST"])
def update_bulk():
    data = request.get_json()
    indices = [int(i) for i in data.get("indices", [])]
    key = data.get("key", "")
    value = data.get("value", "")
    if key not in ("banca", "categoria", "causale", "tipo"):
        return jsonify({"error": "Campo non consentito"}), 400
    records = load_csv()
    for idx in indices:
        if 0 <= idx < len(records):
            records[idx][key] = value
    save_csv(records)
    return jsonify({"ok": True})


@app.route("/api/transactions/add", methods=["POST"])
def add_transaction():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Dati mancanti"}), 400
    data_val = str(data.get("data", "")).strip()
    if not data_val:
        return jsonify({"error": "Data obbligatoria"}), 400
    records = load_csv()
    new_record = {
        "data":        data_val,
        "causale":     str(data.get("causale", "")).strip(),
        "descrizione": str(data.get("descrizione", "")).strip(),
        "importo":     round(float(data.get("importo") or 0), 2),
        "categoria":   str(data.get("categoria", "?")).strip(),
        "banca":       str(data.get("banca", "Trade Republic")).strip(),
        "tipo":        str(data.get("tipo", "spesa")).strip(),
    }
    records.append(new_record)
    records.sort(key=lambda r: str(r.get("data", "")), reverse=True)
    save_csv(records)
    return jsonify({"ok": True})


@app.route("/api/categories", methods=["GET"])
def get_categories():
    return jsonify({"categories": load_categories()})


@app.route("/api/categories", methods=["POST"])
def add_category():
    data = request.get_json()
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Nome mancante"}), 400
    cats = load_categories()
    if name not in cats:
        cats.append(name)
        save_categories_file(cats)
    return jsonify({"categories": cats})


@app.route("/api/onboarding", methods=["GET"])
def get_onboarding():
    return jsonify(load_onboarding())


@app.route("/api/onboarding", methods=["POST"])
def post_onboarding():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Dati mancanti"}), 400
    save_onboarding_file(data)
    return jsonify({"ok": True})


@app.route("/api/dashboard", methods=["GET"])
def dashboard():
    month = request.args.get("month", datetime.now().strftime("%Y-%m"))
    f_banca = request.args.get("banca") or None        # cross-filter: banca
    f_cat   = request.args.get("categoria") or None     # cross-filter: categoria
    records = load_csv()

    # Monthly spending by category (exclude Investimento and Entrata)
    month_records = [r for r in records if str(r.get("data", "")).startswith(month)]
    spese_all = [r for r in month_records if r.get("tipo") == "spesa" and r.get("categoria") not in EXCLUDE_CATS]
    # La torta si filtra SOLO per banca (la categoria evidenzia, non collassa la torta)
    spese = [r for r in spese_all if (not f_banca or r.get("banca") == f_banca)]
    by_categoria = {}
    for r in spese:
        cat = r.get("categoria", "?")
        by_categoria[cat] = round(by_categoria.get(cat, 0) + abs(float(r.get("importo", 0))), 2)
    total_spese = round(sum(by_categoria.values()), 2)
    # Spesa della categoria filtrata (rispetta la banca attiva) — per la lettura "Spese · X"
    spese_categoria = (round(sum(abs(float(r.get("importo", 0))) for r in spese if r.get("categoria") == f_cat), 2)
                       if f_cat else None)

    # Per-category, per-bank breakdown (for pie popup)
    spese_per_cat_banca = {}
    for r in spese:
        cat   = r.get("categoria", "?")
        banca = r.get("banca", "?")
        if cat not in spese_per_cat_banca:
            spese_per_cat_banca[cat] = {}
        spese_per_cat_banca[cat][banca] = round(
            spese_per_cat_banca[cat].get(banca, 0) + abs(float(r.get("importo", 0))), 2
        )

    # Per-account summary for current month (tipo-based for both banks)
    def account_month_summary(banca):
        txs = [r for r in month_records if r.get("banca") == banca]
        entrate = sum(float(r.get("importo", 0)) for r in txs if r.get("tipo") == "entrata" and r.get("categoria") not in EXCLUDE_CATS)
        uscite  = sum(abs(float(r.get("importo", 0))) for r in txs if r.get("tipo") == "spesa" and r.get("categoria") not in EXCLUDE_CATS)
        uscite_cat = (round(sum(abs(float(r.get("importo", 0))) for r in txs
                                if r.get("tipo") == "spesa" and r.get("categoria") == f_cat), 2)
                      if f_cat else None)
        return {
            "entrate": round(entrate, 2),
            "uscite":  round(uscite, 2),
            "saldo":   round(entrate - uscite, 2),
            "uscite_cat": uscite_cat,
        }

    banca_generali = account_month_summary("Banca Generali")
    trade_republic  = account_month_summary("Trade Republic")

    # Deposito stimato per banca (onboarding + movimenti dalla data in poi)
    onboarding = load_onboarding()

    # Ultimo giorno del mese visualizzato (es. "2026-01" → "2026-01-31")
    try:
        _y, _m = int(month[:4]), int(month[5:7])
        max_date = f"{month}-{calendar.monthrange(_y, _m)[1]:02d}"
    except Exception:
        max_date = None

    def calc_deposito_stimato(banca, cutoff=None):
        onb = onboarding.get(banca, {})
        onb_amount = float(onb.get("amount") or 0)
        onb_date   = str(onb.get("date") or "").strip()
        if not onb_date:
            return None   # non configurato
        filtered = [r for r in records
                    if r.get("banca") == banca and str(r.get("data", "")) >= onb_date
                    and (cutoff is None or str(r.get("data", "")) <= cutoff)]
        return round(onb_amount + sum(float(r.get("importo", 0)) for r in filtered), 2)

    deposito_stimato_bg = calc_deposito_stimato("Banca Generali", cutoff=max_date)
    deposito_stimato_tr = calc_deposito_stimato("Trade Republic", cutoff=max_date)

    # Deposito patrimoniale = somma stime BG + TR (None se non configurate)
    deposito_patrimoniale = round(
        (deposito_stimato_bg or 0) + (deposito_stimato_tr or 0), 2
    )

    # ── Valore Investimenti (Overview Patrimonio: sum S+ETF for displayed month,
    #    fallback to most recent available month if current month has no data) ──
    investments_dash = load_investments()
    inv_entities_dash = investments_dash.get("entities", [])

    def get_valore_investimenti(entities, month_str):
        available = set()
        for e in entities:
            if (e.get("type") or "?").upper() in ("S", "ETF"):
                for ms, v in (e.get("values") or {}).items():
                    if ms <= month_str and float(v or 0) > 0:
                        available.add(ms)
        if not available:
            return None
        target = max(available)
        total = sum(
            float((e.get("values") or {}).get(target, 0) or 0)
            for e in entities
            if (e.get("type") or "?").upper() in ("S", "ETF")
        )
        return round(total, 2)

    valore_investimenti = get_valore_investimenti(inv_entities_dash, month)

    # ── Controvalore di carico ────────────────────────────────────────────────
    # = importo "Investimento Iniziale" in onboarding
    #   + somma algebrica di tutte le transazioni tipo=="investimento"
    #     dalla data di onboarding fino all'ultimo giorno del mese visualizzato
    inv_ini_onb    = onboarding.get("Investimento Iniziale") or onboarding.get("Altro") or {}
    inv_ini_amount = float(inv_ini_onb.get("amount") or 0)
    inv_ini_date   = str(inv_ini_onb.get("date") or "").strip()

    # Se non è impostata la data di partenza, usa tutte le transazioni fino al mese
    inv_txs = [
        r for r in records
        if r.get("tipo") == "investimento"
        and (not inv_ini_date or str(r.get("data", "")) >= inv_ini_date)
        and (max_date is None or str(r.get("data", "")) <= max_date)
    ]
    # Acquisto (importo < 0) → aumenta il carico (abs)
    # Vendita  (importo > 0) → riduce  il carico (sottrai)
    # Equivalente a: somma(-importo) per ogni transazione
    controvalore_carico = round(
        inv_ini_amount - sum(float(r.get("importo", 0)) for r in inv_txs), 2
    )

    # Investimenti by month
    invest_records = [r for r in records if r.get("tipo") == "investimento"]
    invest_by_month = {}
    for r in invest_records:
        m = str(r.get("data", ""))[:7]
        invest_by_month[m] = round(invest_by_month.get(m, 0) + float(r.get("importo", 0)), 2)

    sorted_months = sorted(invest_by_month.keys())
    invest_monthly = [{"month": m, "importo": invest_by_month[m]} for m in sorted_months]
    totale_investito = round(sum(invest_by_month.values()), 2)   # negative sum
    investiti_abs = round(abs(totale_investito), 2)

    # Cumulative invest
    cumulative = 0
    for item in invest_monthly:
        cumulative += item["importo"]
        item["cumulativo"] = round(cumulative, 2)

    # Monthly trend across ALL months (for trend chart)
    all_months = sorted({str(r.get("data", ""))[:7] for r in records if r.get("data")})
    monthly_trend = []
    for m in all_months:
        m_recs = [r for r in records if str(r.get("data", "")).startswith(m)]
        spese_m = round(sum(abs(float(r.get("importo", 0))) for r in m_recs if r.get("tipo") == "spesa" and r.get("categoria") not in EXCLUDE_CATS), 2)
        invest_m = round(sum(abs(float(r.get("importo", 0))) for r in m_recs if r.get("tipo") == "investimento"), 2)
        monthly_trend.append({"month": m, "spese": spese_m, "investimento": invest_m})

    return jsonify({
        "month": month,
        "spese_per_categoria": by_categoria,
        "spese_per_cat_banca": spese_per_cat_banca,
        "total_spese": total_spese,
        "filtered": {"categoria": f_cat, "banca": f_banca, "spese_categoria": spese_categoria},
        "invest_monthly": invest_monthly,
        "totale_investito": totale_investito,
        "investiti_abs": investiti_abs,
        "banca_generali": banca_generali,
        "trade_republic": trade_republic,
        "deposito_stimato_bg": deposito_stimato_bg,
        "deposito_stimato_tr": deposito_stimato_tr,
        "patrimonio": {
            "deposito":  deposito_patrimoniale,
            "investiti": valore_investimenti,
            "totale":    round(deposito_patrimoniale + (valore_investimenti or 0), 2),
        },
        "valore_investimenti": valore_investimenti,
        "controvalore_carico": controvalore_carico,
        "monthly_trend": monthly_trend,
        "available_months": all_months,
    })


@app.route("/api/export", methods=["GET"])
def export_csv():
    month = request.args.get("month")
    categoria = request.args.get("categoria")
    banca = request.args.get("banca")
    tipo = request.args.get("tipo")
    q = (request.args.get("q") or "").strip().lower()
    ids = request.args.get("ids")  # comma-separated indices for session export

    records = load_csv()

    if ids:
        idx_list = [int(i) for i in ids.split(",") if i.isdigit()]
        records = [records[i] for i in idx_list if i < len(records)]
    else:
        if month:
            records = [r for r in records if str(r.get("data", "")).startswith(month)]
        if categoria:
            records = [r for r in records if r.get("categoria") == categoria]
        if banca:
            records = [r for r in records if r.get("banca") == banca]
        if tipo:
            records = [r for r in records if r.get("tipo") == tipo]
        if q:
            records = [r for r in records
                       if q in str(r.get("descrizione", "")).lower()
                       or q in str(r.get("causale", "")).lower()
                       or q in str(r.get("categoria", "")).lower()]

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=CSV_COLUMNS)
    writer.writeheader()
    for r in records:
        writer.writerow({k: r.get(k, "") for k in CSV_COLUMNS})

    output.seek(0)
    return send_file(
        io.BytesIO(output.read().encode("utf-8")),
        mimetype="text/csv",
        as_attachment=True,
        download_name="transazioni.csv",
    )


@app.route("/api/months", methods=["GET"])
def get_months():
    records = load_csv()
    months = sorted({str(r.get("data", ""))[:7] for r in records if r.get("data")}, reverse=True)
    return jsonify({"months": months})


@app.route("/api/cash-movements/check", methods=["GET"])
def check_cash_movements():
    """
    Find unpaired cash movements.
    A valid pair = one outgoing (importo < 0) + one incoming (importo > 0)
    with matching absolute amount (±1 EUR) and close dates (≤5 days apart).
    Returns the _idx list of movements that have no counterpart.
    """
    from datetime import datetime as dt

    records = load_csv()

    cash_mvs = [
        {
            "_idx":   i,
            "importo": float(r.get("importo", 0)),
            "data":    str(r.get("data", "")),
        }
        for i, r in enumerate(records)
        if r.get("tipo") == "cash movement"
    ]

    outgoing = [m for m in cash_mvs if m["importo"] < 0]
    incoming = [m for m in cash_mvs if m["importo"] > 0]

    matched_in  = set()
    matched_out = set()

    # Greedy: for each outgoing find the best-matching incoming
    for out in sorted(outgoing, key=lambda x: x["data"]):
        try:
            out_date = dt.strptime(out["data"], "%Y-%m-%d")
        except Exception:
            continue

        best       = None
        best_score = float("inf")

        for inc in incoming:
            if inc["_idx"] in matched_in:
                continue
            try:
                inc_date = dt.strptime(inc["data"], "%Y-%m-%d")
            except Exception:
                continue

            date_gap   = abs((out_date - inc_date).days)
            amount_gap = abs(abs(out["importo"]) - abs(inc["importo"]))

            if date_gap <= 20 and amount_gap <= 1.0:
                score = date_gap + amount_gap
                if score < best_score:
                    best_score = score
                    best = inc

        if best:
            matched_in.add(best["_idx"])
            matched_out.add(out["_idx"])

    unmatched = [
        m["_idx"] for m in cash_mvs
        if m["_idx"] not in matched_in and m["_idx"] not in matched_out
    ]

    return jsonify({"unmatched": unmatched, "total_cash_mvs": len(cash_mvs)})


@app.route("/api/trend", methods=["GET"])
def get_trend():
    """All time-series data for the patrimonio trend chart (deposits, expenses, ETFs, stocks)."""
    records          = load_csv()
    onboarding_data  = load_onboarding()
    investments_data = load_investments()
    today            = datetime.now()

    # Last completed month (today must be ≥ last day of month)
    curr_y, curr_m = today.year, today.month
    last_of_curr   = calendar.monthrange(curr_y, curr_m)[1]
    if today.day >= last_of_curr:
        end_y, end_m = curr_y, curr_m
    else:
        end_y, end_m = (curr_y, curr_m - 1) if curr_m > 1 else (curr_y - 1, 12)

    def is_completed(ms):
        try:
            return (int(ms[:4]), int(ms[5:7])) <= (end_y, end_m)
        except Exception:
            return False

    # ── Deposit estimates (same logic as get_investments) ────────────────────
    BANKS = ["Banca Generali", "Trade Republic"]

    def deposit_for_month(banca, ms):
        if not is_completed(ms):
            return None
        try:
            y, m   = int(ms[:4]), int(ms[5:7])
            cutoff = f"{ms}-{calendar.monthrange(y, m)[1]:02d}"
        except Exception:
            return None
        onb        = onboarding_data.get(banca, {})
        onb_amount = float(onb.get("amount") or 0)
        onb_date   = str(onb.get("date") or "").strip()
        if not onb_date or cutoff < onb_date[:10]:
            return None
        filtered = [r for r in records
                    if r.get("banca") == banca
                    and str(r.get("data", "")) >= onb_date
                    and str(r.get("data", "")) <= cutoff]
        return round(onb_amount + sum(float(r.get("importo", 0)) for r in filtered), 2)

    # Month range: from earliest onboarding date to last completed month
    onb_starts = []
    for b in BANKS:
        d = str(onboarding_data.get(b, {}).get("date") or "").strip()
        if d and len(d) >= 7:
            onb_starts.append((int(d[:4]), int(d[5:7])))

    dep_months = []
    if onb_starts:
        sy, sm = min(onb_starts)
        # Ferma la serie depositi all'ultimo mese con un movimento reale.
        last_mov = _last_movement_month(records, BANKS) or (sy, sm)
        dep_end  = min((end_y, end_m), last_mov)
        y, m   = sy, sm
        while (y, m) <= dep_end:
            dep_months.append(f"{y}-{m:02d}")
            m += 1
            if m > 12:
                m, y = 1, y + 1

    dep_data = {}
    for banca in BANKS:
        bdata = {}
        for ms in dep_months:
            v = deposit_for_month(banca, ms)
            if v is not None:
                bdata[ms] = v
        dep_data[banca] = bdata

    # ── Monthly spese per bank (only completed months, only non-zero) ────────
    tx_months = sorted({
        str(r.get("data", ""))[:7]
        for r in records
        if len(str(r.get("data", ""))) >= 7 and is_completed(str(r.get("data", ""))[:7])
    })
    spese_data = {}
    for banca in BANKS:
        bdata = {}
        for ms in tx_months:
            v = round(sum(
                abs(float(r.get("importo", 0)))
                for r in records
                if str(r.get("data", "")).startswith(ms)
                and r.get("tipo") == "spesa"
                and r.get("categoria") not in EXCLUDE_CATS
                and r.get("banca") == banca
            ), 2)
            if v > 0:
                bdata[ms] = v
        spese_data[banca] = bdata

    # ── Investment series from investments.json ───────────────────────────────
    entities     = investments_data.get("entities", [])
    etf_list     = []    # [{isin, label, data}]
    azioni_acc   = {}    # month → sum of all stock values
    azioni_stocks = []  # [{name, data: {month: value}}] — individual stocks for popup

    for entity in entities:
        typ  = (entity.get("type") or "?").upper()
        name = entity.get("name", "?")
        vals = entity.get("values", {})
        if typ == "ETF":
            edata = {ms: float(v) for ms, v in vals.items()
                     if is_completed(ms) and float(v or 0) > 0}
            if edata:
                etf_list.append({
                    "isin": entity.get("isin", ""),
                    "label": name,
                    "data": edata,
                })
        elif typ == "S":
            sdata = {}
            for ms, v in vals.items():
                if is_completed(ms) and float(v or 0) > 0:
                    fv = float(v)
                    azioni_acc[ms] = round(azioni_acc.get(ms, 0) + fv, 2)
                    sdata[ms] = fv
            if sdata:
                azioni_stocks.append({"name": name, "data": sdata})

    # ── Assemble ordered series list ─────────────────────────────────────────
    ETF_COLORS = ["#3b82f6", "#8b5cf6", "#06b6d4", "#ec4899", "#14b8a6", "#64748b"]
    series = []

    if dep_data.get("Banca Generali"):
        series.append({"key": "dep_bg",   "label": "Deposito BG",
                        "color": "#6c63ff", "data": dep_data["Banca Generali"]})
    if dep_data.get("Trade Republic"):
        series.append({"key": "dep_tr",   "label": "Deposito TR",
                        "color": "#10b981", "data": dep_data["Trade Republic"]})
    if spese_data.get("Banca Generali"):
        series.append({"key": "spese_bg", "label": "Spese BG",
                        "color": "#f97316", "data": spese_data["Banca Generali"]})
    if spese_data.get("Trade Republic"):
        series.append({"key": "spese_tr", "label": "Spese TR",
                        "color": "#ef4444", "data": spese_data["Trade Republic"]})
    for i, etf in enumerate(etf_list):
        series.append({
            "key":   f"etf_{etf['isin'] or i}",
            "label": etf["label"],
            "color": ETF_COLORS[i % len(ETF_COLORS)],
            "data":  etf["data"],
        })
    if azioni_acc:
        series.append({"key": "azioni", "label": "Azioni",
                        "color": "#f59e0b", "data": azioni_acc,
                        "stocks": azioni_stocks})

    return jsonify({"series": series})


@app.route("/api/investments", methods=["GET"])
def get_investments():
    data = load_investments()

    # ── Compute historical bank-deposit estimates for every completed month ──
    records        = load_csv()
    onboarding_inv = load_onboarding()
    today          = datetime.now()

    # A month is "complete" when its last day has already passed
    curr_y, curr_m = today.year, today.month
    last_of_curr   = calendar.monthrange(curr_y, curr_m)[1]
    if today.day >= last_of_curr:
        end_y, end_m = curr_y, curr_m
    else:
        end_y, end_m = (curr_y, curr_m - 1) if curr_m > 1 else (curr_y - 1, 12)

    def deposit_for_month(banca, month_str):
        """Deposito stimato al'ultimo giorno del mese — None se non configurato."""
        try:
            y, m   = int(month_str[:4]), int(month_str[5:7])
            cutoff = f"{month_str}-{calendar.monthrange(y, m)[1]:02d}"
        except Exception:
            return None
        onb        = onboarding_inv.get(banca, {})
        onb_amount = float(onb.get("amount") or 0)
        onb_date   = str(onb.get("date") or "").strip()
        if not onb_date or cutoff < onb_date[:10]:
            return None
        filtered = [r for r in records
                    if r.get("banca") == banca
                    and str(r.get("data", "")) >= onb_date
                    and str(r.get("data", "")) <= cutoff]
        return round(onb_amount + sum(float(r.get("importo", 0)) for r in filtered), 2)

    # Earliest onboarding date across both banks → start of range
    DEPOSIT_BANKS = ["Banca Generali", "Trade Republic"]
    onb_starts = []
    for b in DEPOSIT_BANKS:
        d = str(onboarding_inv.get(b, {}).get("date") or "").strip()
        if d and len(d) >= 7:
            onb_starts.append((int(d[:4]), int(d[5:7])))

    deposits = {}
    if onb_starts:
        start_y, start_m = min(onb_starts)
        # Non proiettare oltre l'ultimo mese con un movimento reale: niente
        # depositi "fantasma" per mesi chiusi ma senza dati caricati.
        last_mov = _last_movement_month(records, DEPOSIT_BANKS) or (start_y, start_m)
        dep_end  = min((end_y, end_m), last_mov)
        for banca in DEPOSIT_BANKS:
            y, m    = start_y, start_m
            b_vals  = {}
            while (y, m) <= dep_end:
                ms  = f"{y}-{m:02d}"
                val = deposit_for_month(banca, ms)
                if val is not None:
                    b_vals[ms] = val
                m += 1
                if m > 12:
                    m, y = 1, y + 1
            if b_vals:
                deposits[banca] = b_vals

    return jsonify({**data, "deposits": deposits})


@app.route("/api/investments", methods=["POST"])
def post_investments():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Dati mancanti"}), 400
    save_investments(data)
    return jsonify({"ok": True})


@app.route("/api/investments/parse-pdf", methods=["POST"])
def parse_investment_pdf_route():
    if "file" not in request.files:
        return jsonify({"error": "Nessun file caricato"}), 400
    file_bytes = request.files["file"].read()
    try:
        result = parse_investment_pdf(file_bytes)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify(result)


@app.route("/api/investments/merge", methods=["POST"])
def merge_investments():
    body = request.get_json()
    if not body:
        return jsonify({"error": "Dati mancanti"}), 400
    month     = body.get("month", "")
    positions = body.get("positions", [])
    if not month or not positions:
        return jsonify({"error": "month e positions richiesti"}), 400

    inv = load_investments()
    # Index by ISIN — the stable cross-month identifier
    by_isin = {e.get("isin", ""): e for e in inv.get("entities", [])}

    for pos in positions:
        isin = pos["isin"]
        if isin in by_isin:
            by_isin[isin].setdefault("values", {})[month] = pos["value"]
        else:
            by_isin[isin] = {
                "isin":   isin,
                "name":   pos["name"],
                "type":   pos["type"],
                "values": {month: pos["value"]},
            }

    inv["entities"] = list(by_isin.values())
    save_investments(inv)
    return jsonify({"ok": True, "count": len(positions)})


# ─────────────────────────────────────────────────────────────────────────────
# v2.2 — helper condivisi + nuove schermate (Andamento, Allocazione/Rischio) + BI
# ─────────────────────────────────────────────────────────────────────────────

CASH_BANKS   = ["Banca Generali", "Trade Republic"]
EXCLUDE_CATS = ("Rimborso Altri", "Rimborso Papà")   # rimborsi esclusi da spese/entrate nelle statistiche (PAC è tipo="investimento", già fuori da spese/entrate)


def _completed_month_end():
    """(anno, mese) dell'ultimo mese 'completo' (ultimo giorno già passato)."""
    today = datetime.now()
    last = calendar.monthrange(today.year, today.month)[1]
    if today.day >= last:
        return today.year, today.month
    return (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)


def _last_movement_month(records, banks):
    """(anno, mese) dell'ultimo mese con un movimento reale per una delle `banks`.
    None se non ci sono movimenti. Serve a NON proiettare il deposito stimato su
    mesi senza dati caricati (es. non mostrare un deposito di 'giugno' quando
    l'ultimo movimento registrato è di maggio)."""
    months = [str(r.get("data", ""))[:7] for r in records
              if r.get("banca") in banks and len(str(r.get("data", ""))) >= 7]
    if not months:
        return None
    last = max(months)
    return int(last[:4]), int(last[5:7])


def _deposit_for_month(records, onboarding, banca, month):
    """Deposito stimato (cash) di `banca` a fine `month` (YYYY-MM). None se non configurato."""
    try:
        y, m = int(month[:4]), int(month[5:7])
        cutoff = f"{month}-{calendar.monthrange(y, m)[1]:02d}"
    except Exception:
        return None
    onb = onboarding.get(banca, {})
    amt = float(onb.get("amount") or 0)
    onb_date = str(onb.get("date") or "").strip()
    if not onb_date or cutoff < onb_date[:10]:
        return None
    filt = [r for r in records if r.get("banca") == banca
            and str(r.get("data", "")) >= onb_date and str(r.get("data", "")) <= cutoff]
    return round(amt + sum(float(r.get("importo", 0)) for r in filt), 2)


def _first_lot_dates(lots):
    """{isin: prima data di acquisto/vendita} — usata come 'data sottoscrizione'."""
    out = {}
    for l in lots:
        isin = l.get("isin"); d = str(l.get("date", ""))
        if isin and d and (isin not in out or d < out[isin]):
            out[isin] = d
    return out


@app.route("/api/networth", methods=["GET"])
def api_networth():
    """Patrimonio totale (cash + investimenti) per mese + headline aumento assoluto."""
    records    = load_csv()
    onboarding = load_onboarding()
    inv        = load_investments()
    end_y, end_m = _completed_month_end()

    starts = []
    for b in CASH_BANKS:
        d = str(onboarding.get(b, {}).get("date") or "").strip()
        if len(d) >= 7:
            starts.append((int(d[:4]), int(d[5:7])))
    cash_by_month = {}
    if starts:
        y, m = min(starts)
        # Ferma la liquidità stimata all'ultimo mese con un movimento reale.
        last_mov = _last_movement_month(records, CASH_BANKS) or (y, m)
        dep_end  = min((end_y, end_m), last_mov)
        while (y, m) <= dep_end:
            ms = f"{y}-{m:02d}"
            cash_by_month[ms] = round(
                sum((_deposit_for_month(records, onboarding, b, ms) or 0) for b in CASH_BANKS), 2)
            m += 1
            if m > 12:
                m, y = 1, y + 1

    invest_by_month = {}
    for e in inv.get("entities", []):
        if (e.get("type") or "?").upper() in ("S", "ETF"):
            for ms, v in (e.get("values") or {}).items():
                if float(v or 0) > 0:
                    invest_by_month[ms] = round(invest_by_month.get(ms, 0) + float(v), 2)

    series = metrics.networth_series(cash_by_month, invest_by_month)
    headline = {}
    if series:
        first, last = series[0], series[-1]
        aumento = round(last["totale"] - first["totale"], 2)
        headline = {
            "valore_attuale":  last["totale"],
            "valore_iniziale": first["totale"],
            "aumento_assoluto": aumento,
            "variazione_pct":  round(aumento / first["totale"] * 100, 2) if first["totale"] else None,
            "mese_iniziale":   first["month"],
            "mese_attuale":    last["month"],
        }
    return jsonify({"series": series, "headline": headline})


@app.route("/api/allocation", methods=["GET"])
def api_allocation():
    """Allocazione per classe + diversificazione (HHI) + volatilità + livelli di rischio."""
    records    = load_csv()
    onboarding = load_onboarding()
    inv        = load_investments()
    store      = load_investment_lots()
    led        = build_ledger(store.get("lots", []), store.get("opening_positions", []))
    isins      = [p["isin"] for p in led["positions"]]
    snap_month, vals = _latest_snapshot(inv, isins)
    ey, em = _completed_month_end()
    cutoff_month = f"{ey}-{em:02d}"

    cash = round(sum((_deposit_for_month(records, onboarding, b, cutoff_month) or 0)
                     for b in CASH_BANKS), 2)
    type_by_isin = {e.get("isin"): (e.get("type") or "?").upper() for e in inv.get("entities", [])}

    etf = azioni = 0.0
    per_isin = []
    for p in led["positions"]:
        v = vals.get(p["isin"])
        if v is None:
            continue
        t = type_by_isin.get(p["isin"], "S")
        if t == "ETF":
            etf += v
        else:
            azioni += v
        per_isin.append({"isin": p["isin"], "name": p["name"], "value": round(v, 2),
                         "type": t, "cost_basis": p["cost_basis"]})
    etf, azioni = round(etf, 2), round(azioni, 2)
    patrimonio = round(cash + etf + azioni, 2)

    pie = [s for s in (
        {"label": "Liquidità", "value": cash,   "color": "#10b981"},
        {"label": "ETF",       "value": etf,    "color": "#3b82f6"},
        {"label": "Azioni",    "value": azioni, "color": "#f59e0b"},
    ) if s["value"] > 0]

    def pct(x):
        return round(x / patrimonio * 100, 1) if patrimonio else 0
    classes = {"Liquidità": {"value": cash, "pct": pct(cash)},
               "ETF": {"value": etf, "pct": pct(etf)},
               "Azioni": {"value": azioni, "pct": pct(azioni)}}

    inv_weights = [p["value"] for p in per_isin]
    h = metrics.hhi(inv_weights)
    diversification = {"hhi": round(h, 4) if h else None,
                       "effective_n": round(metrics.effective_n(inv_weights), 2) if h else None,
                       "band": metrics.diversification_band(h)}

    vol_per, prices_by = [], {}
    for e in inv.get("entities", []):
        if e.get("isin") in isins:
            ps = metrics.unit_price_series(e.get("values", {}), store.get("lots", []), e["isin"])
            prices_by[e["isin"]] = ps
            rets = metrics.monthly_returns(ps)
            vol = metrics.annualized_vol(rets)
            vol_per.append({"isin": e["isin"], "name": e.get("name"),
                            "vol": round(vol * 100, 1) if vol is not None else None,
                            "n_returns": len(rets)})
    weights_map = {p["isin"]: p["value"] for p in per_isin}
    pvol = metrics.annualized_vol(metrics.portfolio_monthly_returns(prices_by, weights_map))

    # Rischio: con pochi mesi di dati si stima per classe di attività (più stabile)
    buckets = {lvl: {"level": lvl, "value": 0.0, "holdings": []} for lvl in metrics.RISK_ORDER}
    if cash > 0:
        buckets["Basso"]["value"] += cash
        buckets["Basso"]["holdings"].append("Liquidità")
    for p in per_isin:
        lvl = metrics.assetclass_risk_fallback(p["type"])
        buckets[lvl]["value"] += p["value"]
        buckets[lvl]["holdings"].append(p["name"])
    risk = []
    for lvl in metrics.RISK_ORDER:
        b = buckets[lvl]
        b["value"] = round(b["value"], 2)
        b["pct"] = pct(b["value"])
        risk.append(b)

    return jsonify({
        "patrimonio": patrimonio, "snapshot_month": snap_month,
        "holdings": per_isin,
        "allocation": {"pie": pie, "classes": classes},
        "diversification": diversification,
        "volatility": {"portfolio": round(pvol * 100, 1) if pvol is not None else None,
                       "per_isin": vol_per,
                       "note": "Stima indicativa: basata su pochi mesi di dati, migliora nel tempo."},
        "risk_buckets": risk,
        "risk_note": "Livello stimato per classe di attività (storico ancora breve per la volatilità)."},
    )


def _bi_context():
    """Aggregati compatti dei dati finanziari per il modello (sola lettura, pochi token)."""
    records = load_csv()
    onboarding = load_onboarding()
    inv = load_investments()
    store = load_investment_lots()
    led = build_ledger(store.get("lots", []), store.get("opening_positions", []))
    isins = [p["isin"] for p in led["positions"]]
    snap_month, vals = _latest_snapshot(inv, isins)
    ey, em = _completed_month_end()
    cutoff = f"{ey}-{em:02d}"

    # spese per categoria per mese + entrate per mese (escludendo rimborsi)
    months = sorted({str(r.get("data", ""))[:7] for r in records if r.get("data")})
    spese_cat_mese, entrate_mese = {}, {}
    for r in records:
        m = str(r.get("data", ""))[:7]
        cat = r.get("categoria", "?")
        if cat in EXCLUDE_CATS:
            continue
        if r.get("tipo") == "spesa":
            spese_cat_mese.setdefault(m, {})
            spese_cat_mese[m][cat] = round(spese_cat_mese[m].get(cat, 0) + abs(float(r.get("importo", 0))), 2)
        elif r.get("tipo") == "entrata":
            entrate_mese[m] = round(entrate_mese.get(m, 0) + float(r.get("importo", 0)), 2)

    # Flussi di investimento (tipo=="investimento"): quanto è stato VERSATO (acquisti,
    # importo<0) e DISINVESTITO (vendite, importo>0), per mese e per anno. Serve a
    # rispondere a "quanto ho investito nel 2026" = somma delle uscite di tipo investimento.
    flussi_mese = {}
    for r in records:
        if r.get("tipo") != "investimento":
            continue
        m = str(r.get("data", ""))[:7]
        if not m:
            continue
        imp = float(r.get("importo", 0))
        d = flussi_mese.setdefault(m, {"versato": 0.0, "disinvestito": 0.0, "netto": 0.0})
        if imp < 0:
            d["versato"] += -imp
        else:
            d["disinvestito"] += imp
        d["netto"] += imp
    flussi_anno = {}
    for m, d in flussi_mese.items():
        a = flussi_anno.setdefault(m[:4], {"versato": 0.0, "disinvestito": 0.0, "netto": 0.0})
        for k in ("versato", "disinvestito", "netto"):
            a[k] += d[k]
    for d in list(flussi_mese.values()) + list(flussi_anno.values()):
        for k in d:
            d[k] = round(d[k], 2)

    positions = []
    for p in led["positions"]:
        v = vals.get(p["isin"])
        positions.append({"titolo": p["name"], "quote": p["shares"], "prezzo_medio": p["avg_cost"],
                          "carico": p["cost_basis"], "valore_attuale": v,
                          "guadagno": round(v - p["cost_basis"], 2) if v is not None else None})
    cash = {b: _deposit_for_month(records, onboarding, b, cutoff) for b in CASH_BANKS}
    return {
        "valuta": "EUR", "mese_dati_investimenti": snap_month,
        "liquidita_per_banca": cash,
        "spese_per_categoria_per_mese": spese_cat_mese,
        "entrate_per_mese": entrate_mese,
        "investimenti": positions,
        "flussi_investimenti_per_mese": flussi_mese,
        "flussi_investimenti_per_anno": flussi_anno,
        "mesi_disponibili": months,
    }


@app.route("/api/ask", methods=["POST"])
def api_ask():
    """'Chiedi alla tua BI' — risponde in linguaggio naturale dai tuoi dati (sola lettura)."""
    body = request.get_json(silent=True) or {}
    question = (body.get("question") or "").strip()
    if not question:
        return jsonify({"error": "Scrivi una domanda"}), 400
    try:
        import ai_bi
        answer = ai_bi.answer_question(question, _bi_context())
        return jsonify({"answer": answer})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"Errore: {e}"}), 500


@app.route("/api/reimbursements/suggest", methods=["POST"])
def reimbursements_suggest():
    """L'AI propone: quali entrate sono rimborsi e quali spese sono quelle rimborsate.
    NON applica nulla — l'utente conferma e poi usa /api/transactions/update-bulk."""
    body  = request.get_json(silent=True) or {}
    text  = (body.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Scrivi cosa ti è stato rimborsato"}), 400

    records = [dict(r, _idx=i) for i, r in enumerate(load_csv())]
    incoming = sorted(
        [{"_idx": r["_idx"], "data": r.get("data"), "descrizione": r.get("descrizione"),
          "importo": float(r.get("importo", 0)), "categoria": r.get("categoria")}
         for r in records
         if r.get("tipo") == "entrata" and r.get("categoria") != "Paghetta" and r.get("categoria") not in EXCLUDE_CATS],
        key=lambda x: x["data"] or "", reverse=True)[:40]
    spese = sorted(
        [{"_idx": r["_idx"], "data": r.get("data"), "descrizione": r.get("descrizione"),
          "importo": float(r.get("importo", 0)), "categoria": r.get("categoria")}
         for r in records
         if r.get("tipo") == "spesa" and r.get("categoria") not in EXCLUDE_CATS],
        key=lambda x: x["data"] or "", reverse=True)[:120]

    try:
        import ai_reimburse
        prop = ai_reimburse.suggest(text, incoming, spese)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"Errore AI: {e}"}), 500

    by_idx = {r["_idx"]: r for r in records}

    def enrich(idx_list):
        out = []
        for i in idx_list:
            r = by_idx.get(i)
            if r:
                out.append({"_idx": i, "data": r.get("data"), "descrizione": r.get("descrizione"),
                            "importo": float(r.get("importo", 0)), "categoria": r.get("categoria")})
        return out

    inc = enrich(prop.get("incoming_idx", []))
    exp = enrich(prop.get("expense_idx", []))
    return jsonify({
        "incoming": inc, "expenses": exp, "nota": prop.get("nota", ""),
        "tot_incoming": round(sum(abs(x["importo"]) for x in inc), 2),
        "tot_expenses": round(sum(abs(x["importo"]) for x in exp), 2),
    })


# ── Rimborsi "al volo" (pre-note dal telefono) ────────────────────────────────
# Durante il mese annoti una spesa da farti rimborsare; a fine mese, con l'estratto
# conto importato, abbini la pre-nota alla transazione reale (l'AI Rimborsi resta separata).

def load_rimborsi_pending():
    if not os.path.exists(RIMBORSI_PENDING_PATH):
        return []
    reason = None
    try:
        with open(RIMBORSI_PENDING_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        reason = "contenuto non valido (non è una lista)"
    except Exception as e:
        reason = str(e)
    # File corrotto/inatteso: NON azzerarlo in silenzio (il prossimo save lo
    # sovrascriverebbe perdendo dati recuperabili). Mettilo da parte come .bak.
    try:
        bak = RIMBORSI_PENDING_PATH + ".corrupt-" + datetime.now().strftime("%Y%m%d%H%M%S") + ".bak"
        shutil.move(RIMBORSI_PENDING_PATH, bak)
        app.logger.warning("rimborsi_pending.json non valido (%s) → salvato in %s", reason, bak)
    except Exception:
        pass
    return []


def save_rimborsi_pending(items):
    backup_data()
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(RIMBORSI_PENDING_PATH, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2, allow_nan=False)


def _candidates_for(note, records):
    """Transazioni spesa con importo simile (±0,50€), per abbinare la pre-nota a fine mese."""
    try:
        tgt = abs(float(note.get("importo") or 0))
    except Exception:
        return []
    out = []
    for i, r in enumerate(records):
        if r.get("tipo") != "spesa":
            continue
        try:
            amt = abs(float(r.get("importo") or 0))
        except Exception:
            continue
        if abs(amt - tgt) <= 0.5:
            out.append({"_idx": i, "data": r.get("data"), "descrizione": r.get("descrizione"),
                        "importo": amt, "categoria": r.get("categoria"), "banca": r.get("banca")})
    out.sort(key=lambda c: (abs(c["importo"] - tgt), str(c.get("data", ""))))
    return out[:5]


@app.route("/api/rimborsi-pending", methods=["GET"])
def get_rimborsi_pending():
    items = load_rimborsi_pending()
    if request.args.get("suggest") == "1":
        records = load_csv()
        for it in items:
            if not it.get("matched"):
                it["candidates"] = _candidates_for(it, records)
    return jsonify({"items": items})


@app.route("/api/rimborsi-pending", methods=["POST"])
def add_rimborso_pending():
    data = request.get_json(silent=True) or {}
    imp = data.get("importo")
    if imp in (None, ""):
        return jsonify({"error": "Importo obbligatorio"}), 400
    if isinstance(imp, bool):
        return jsonify({"error": "Importo non valido"}), 400
    try:
        imp = round(abs(float(imp)), 2)
    except Exception:
        return jsonify({"error": "Importo non valido"}), 400
    if not math.isfinite(imp):   # blocca Infinity / NaN (corromperebbero il JSON)
        return jsonify({"error": "Importo non valido"}), 400
    items = load_rimborsi_pending()
    item = {
        "id":          uuid.uuid4().hex[:12],
        "data":        str(data.get("data") or datetime.now().strftime("%Y-%m-%d")).strip(),
        "descrizione": str(data.get("descrizione") or "").strip(),
        "importo":     imp,
        "categoria":   str(data.get("categoria") or "").strip(),
        "nota":        str(data.get("nota") or "").strip(),
        "created_at":  datetime.now().isoformat(timespec="seconds"),
        "matched":     False,
    }
    items.append(item)
    save_rimborsi_pending(items)
    return jsonify({"ok": True, "item": item})


@app.route("/api/rimborsi-pending/<rid>", methods=["DELETE"])
def delete_rimborso_pending(rid):
    items = [x for x in load_rimborsi_pending() if x.get("id") != rid]
    save_rimborsi_pending(items)
    return jsonify({"ok": True})


@app.route("/api/rimborsi-pending/<rid>/toggle", methods=["POST"])
def toggle_rimborso_pending(rid):
    items = load_rimborsi_pending()
    found = False
    for x in items:
        if x.get("id") == rid:
            x["matched"] = not x.get("matched", False)
            found = True
    if not found:
        return jsonify({"error": "Non trovato"}), 404
    save_rimborsi_pending(items)
    return jsonify({"ok": True})


if __name__ == "__main__":
    ensure_csv()
    # Debug OFF di default (istanza always-on); attivabile con DASHBOARD_DEBUG=1
    debug = os.environ.get("DASHBOARD_DEBUG", "").strip() in ("1", "true", "True")
    app.run(debug=debug, port=5002, use_reloader=False)
