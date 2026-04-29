import os
import sys
import csv
import json
import io
import calendar
from datetime import datetime

import pandas as pd
from flask import Flask, request, jsonify, send_from_directory, send_file, abort

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"), override=True)

from file_parser import parse_file
from ai_categorizer import categorize_transactions
from investment_parser import parse_investment_pdf

app = Flask(
    __name__,
    template_folder=os.path.join(os.path.dirname(__file__), "..", "templates"),
    static_folder=os.path.join(os.path.dirname(__file__), "..", "static"),
)

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
CSV_PATH = os.path.join(DATA_DIR, "transactions.csv")
CATEGORIES_PATH = os.path.join(DATA_DIR, "categories.json")
ONBOARDING_PATH = os.path.join(DATA_DIR, "onboarding.json")
INVESTMENTS_PATH = os.path.join(DATA_DIR, "investments.json")

ONBOARDING_BANKS = ["Trade Republic", "Banca Generali", "PayPal", "Investimento Iniziale"]

CSV_COLUMNS = ["data", "causale", "descrizione", "importo", "categoria", "banca", "tipo"]

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
    with open(CATEGORIES_PATH, "w", encoding="utf-8") as f:
        json.dump(cats, f, ensure_ascii=False, indent=2)


def load_onboarding():
    if os.path.exists(ONBOARDING_PATH):
        with open(ONBOARDING_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {b: {"amount": 0, "date": ""} for b in ONBOARDING_BANKS}


def save_onboarding_file(data):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(ONBOARDING_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_investments():
    if os.path.exists(INVESTMENTS_PATH):
        with open(INVESTMENTS_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"entities": []}


def save_investments(data):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(INVESTMENTS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


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
        df["importo"] = pd.to_numeric(df["importo"], errors="coerce").fillna(0)
        return df.to_dict(orient="records")
    except pd.errors.EmptyDataError:
        return []


def save_csv(records):
    ensure_csv()
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for r in records:
            row = {k: r.get(k, "") for k in CSV_COLUMNS}
            writer.writerow(row)


def dedup_key(t):
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

    added = 0
    for t in new_txs:
        k = dedup_key(t)
        if k not in existing_keys:
            clean = {col: t.get(col, "") for col in CSV_COLUMNS}
            clean["importo"] = round(float(clean.get("importo") or 0), 2)
            existing.append(clean)
            existing_keys.add(k)
            added += 1

    save_csv(existing)
    return jsonify({"saved": added, "total": len(existing)})


@app.route("/api/transactions", methods=["GET"])
def get_transactions():
    month = request.args.get("month")
    categoria = request.args.get("categoria")
    banca = request.args.get("banca")
    tipo = request.args.get("tipo")

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
    records = load_csv()

    # Monthly spending by category (exclude Investimento and Entrata)
    month_records = [r for r in records if str(r.get("data", "")).startswith(month)]
    spese = [r for r in month_records if r.get("tipo") == "spesa"]
    by_categoria = {}
    for r in spese:
        cat = r.get("categoria", "?")
        by_categoria[cat] = round(by_categoria.get(cat, 0) + abs(float(r.get("importo", 0))), 2)
    total_spese = round(sum(by_categoria.values()), 2)

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
        entrate = sum(float(r.get("importo", 0)) for r in txs if r.get("tipo") == "entrata")
        uscite  = sum(abs(float(r.get("importo", 0))) for r in txs if r.get("tipo") == "spesa")
        return {
            "entrate": round(entrate, 2),
            "uscite":  round(uscite, 2),
            "saldo":   round(entrate - uscite, 2),
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
        spese_m = round(sum(abs(float(r.get("importo", 0))) for r in m_recs if r.get("tipo") == "spesa"), 2)
        invest_m = round(sum(abs(float(r.get("importo", 0))) for r in m_recs if r.get("tipo") == "investimento"), 2)
        monthly_trend.append({"month": m, "spese": spese_m, "investimento": invest_m})

    return jsonify({
        "month": month,
        "spese_per_categoria": by_categoria,
        "spese_per_cat_banca": spese_per_cat_banca,
        "total_spese": total_spese,
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
        y, m   = sy, sm
        while (y, m) <= (end_y, end_m):
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
        for banca in DEPOSIT_BANKS:
            y, m    = start_y, start_m
            b_vals  = {}
            while (y, m) <= (end_y, end_m):
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


if __name__ == "__main__":
    ensure_csv()
    app.run(debug=True, port=5002)
