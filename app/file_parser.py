import io
import re
import csv
import math
import pandas as pd
import pdfplumber
from datetime import datetime
from dateutil import parser as dateparser


CATEGORIES = ["Pasto", "Caffè", "Merendine", "Alcol", "Spesa", "Mezzi", "Viaggi", "Personali",
              "Acquisti Online", "Investimento", "Paghetta", "Cash Movement"]

MONTH_MAP = {
    'gen':'01','feb':'02','mar':'03','apr':'04','mag':'05','giu':'06',
    'lug':'07','ago':'08','set':'09','ott':'10','nov':'11','dic':'12'
}


def _clean_amount(val):
    if val is None:
        return None
    if isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return None if math.isnan(val) else float(val)
    s = str(val).strip().replace("€", "").replace("\xa0", "").replace(" ", "")
    if not s or s in ("-", "+", "."):
        return None
    negative = s.startswith("-")
    s = s.lstrip("+-").strip()
    comma_pos = s.rfind(",")
    dot_pos = s.rfind(".")
    if comma_pos > dot_pos:
        s = s.replace(".", "").replace(",", ".")
    elif dot_pos > comma_pos:
        s = s.replace(",", "")
    elif comma_pos != -1:
        s = s.replace(",", ".")
    s = re.sub(r"[^\d.]", "", s)
    if not s:
        return None
    try:
        result = float(s)
        return -result if negative else result
    except ValueError:
        return None


def _parse_date(val):
    if val is None:
        return None
    if isinstance(val, float) and math.isnan(val):
        return None
    try:
        if isinstance(val, (datetime, pd.Timestamp)):
            return pd.Timestamp(val).strftime("%Y-%m-%d")
        s = str(val).strip()
        if not s or s == "nan":
            return None
        return dateparser.parse(s, dayfirst=True).strftime("%Y-%m-%d")
    except Exception:
        return None


# ─── TRADE REPUBLIC PDF ───────────────────────────────────────────────────────
#
# Column x-boundaries (measured from actual word positions):
#   DATA      : x < 103
#   TIPO      : 103 ≤ x < 153
#   DESC      : 153 ≤ x < 415
#   IN ENTRATA: 415 ≤ x < 452
#   IN USCITA : 452 ≤ x < 489
#   SALDO     : x ≥ 489
#
# Each transaction spans 3 sub-rows (y-gap ≈ 3.8pt each, total ≈ 7.6pt).
# Gap between transactions ≈ 24pt → split on gap > 15pt.


def _check_saldo_continuity(blocks):
    """
    Verify that every consecutive pair of transaction blocks has a coherent
    saldo progression: saldo[i] == saldo[i-1] + amount[i]  (±0.05 € tolerance).

    Blocks that were skipped (Premio / Interessi / …) are INCLUDED in the check
    so that even rows we don't import are accounted for in the running balance.

    Returns a dict:
        ok       – True if no anomalies found
        checked  – number of consecutive pairs actually verified
        issues   – list of human-readable anomaly descriptions
        found    – total transaction blocks detected in the PDF
        kept     – blocks that will be imported
        skipped  – blocks filtered out (Premio, Interessi, …)
    """
    # Only use blocks that have both saldo and amount (we can't check the rest)
    checkable = [b for b in blocks if b["saldo"] is not None and b["amount"] is not None]

    issues = []
    for i in range(1, len(checkable)):
        prev = checkable[i - 1]
        curr = checkable[i]
        expected = round(prev["saldo"] + curr["amount"], 2)
        diff = round(abs(expected - curr["saldo"]), 2)
        if diff > 0.05:  # 5-cent tolerance for floating-point rounding
            issues.append(
                f"Possibile transazione mancante tra {prev['date']} e {curr['date']}: "
                f"saldo atteso {expected:.2f} €, trovato {curr['saldo']:.2f} € "
                f"(Δ = {diff:.2f} €)"
            )

    return {
        "ok":      len(issues) == 0,
        "checked": max(0, len(checkable) - 1),
        "issues":  issues,
        "found":   len(blocks),
        "kept":    sum(1 for b in blocks if not b["skipped"]),
        "skipped": sum(1 for b in blocks if b["skipped"]),
    }


def parse_trade_republic_pdf(file_bytes, banca="Trade Republic"):
    X_DATA    = 103
    X_TIPO    = 153
    X_DESC    = 415
    X_ENTRATA = 452
    X_USCITA  = 489
    Y_SPLIT   = 15   # gap larger than this → new transaction block

    SKIP_TIPOS = {"Premio", "Interessi", "Imposte", "Rendimento"}

    rows        = []
    recon_blocks = []   # all dated blocks (including skipped) for reconciliation

    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            words = page.extract_words(keep_blank_chars=False)
            if not words:
                continue

            # Round each word's top to nearest integer, then sort
            for w in words:
                w["_y"] = round(w["top"])
            words.sort(key=lambda w: (w["_y"], w["x0"]))

            # Group into y-rows
            y_rows = {}
            for w in words:
                y_rows.setdefault(w["_y"], []).append(w)

            sorted_ys = sorted(y_rows.keys())

            # Merge consecutive y-rows into transaction blocks (split when gap > Y_SPLIT)
            blocks = []
            cur = []
            prev_y = None
            for y in sorted_ys:
                if prev_y is not None and (y - prev_y) > Y_SPLIT:
                    if cur:
                        blocks.append(cur)
                    cur = []
                cur.extend(y_rows[y])
                prev_y = y
            if cur:
                blocks.append(cur)

            # Process each block as one transaction
            for block in blocks:
                def col_words(x_min, x_max):
                    return sorted(
                        [w for w in block if x_min <= w["x0"] < x_max],
                        key=lambda w: (w["_y"], w["x0"])
                    )
                def col_text(x_min, x_max):
                    return " ".join(w["text"] for w in col_words(x_min, x_max)).strip()
                def parse_col_amount(x_min, x_max):
                    text = col_text(x_min, x_max).replace("€", "").replace(".", "")
                    m2 = re.search(r"(\d[\d,]*)", text)
                    if m2:
                        try:
                            return float(m2.group(1).replace(",", "."))
                        except ValueError:
                            pass
                    return None

                # --- Date ---
                data_text = col_text(0, X_DATA)
                m = re.search(
                    r'(\d{1,2})\s+(gen|feb|mar|apr|mag|giu|lug|ago|set|ott|nov|dic)\D{0,10}(\d{4})',
                    data_text, re.I
                )
                if not m:
                    continue
                day      = m.group(1).zfill(2)
                month    = MONTH_MAP[m.group(2).lower()]
                year     = m.group(3)
                date_str = f"{year}-{month}-{day}"

                # --- Tipo ---
                tipo_text = col_text(X_DATA, X_TIPO)
                tipo_text = re.sub(r"^\s*TIPO\s+", "", tipo_text, flags=re.I).strip()
                is_skipped = any(skip in tipo_text for skip in SKIP_TIPOS)

                # --- Amounts (extracted for ALL blocks — needed for reconciliation) ---
                in_entrata = parse_col_amount(X_DESC, X_ENTRATA)
                in_uscita  = parse_col_amount(X_ENTRATA, X_USCITA)
                saldo_val  = parse_col_amount(X_USCITA, 9999)

                # Signed amount for the continuity check
                if in_entrata and not in_uscita:
                    recon_amount = abs(in_entrata)
                elif in_uscita and not in_entrata:
                    recon_amount = -abs(in_uscita)
                elif in_entrata and in_uscita:
                    recon_amount = abs(in_entrata) - abs(in_uscita)
                else:
                    recon_amount = None

                recon_blocks.append({
                    "date":    date_str,
                    "amount":  recon_amount,
                    "saldo":   saldo_val,
                    "skipped": is_skipped,
                })

                # Skip rows we don't want in the ledger
                if is_skipped:
                    continue

                # --- Description ---
                desc_raw = col_text(X_TIPO, X_DESC)
                desc_raw = re.sub(r"^\s*DESCRIZIONE\s+", "", desc_raw, flags=re.I).strip()
                desc = re.sub(r",?\s*exchange\s+rate:.*", "", desc_raw, flags=re.I).strip()
                desc = re.sub(r"\s*\d[\d.,]+,?\s*markup:.*", "", desc, flags=re.I).strip()
                desc = re.sub(r"\s*%null\b.*", "", desc, flags=re.I).strip()
                desc = re.sub(r",\s*[\d,.]+\s*[A-Z]*\$\s*$", "", desc).strip()
                desc = re.sub(r"\([A-Z]{2}\d{2}[A-Z0-9]+\)", "", desc).strip()

                if not desc and not tipo_text:
                    continue

                # --- Determine tipo/importo/categoria ---
                tipo      = "spesa"
                categoria = "?"
                importo   = 0.0
                desc_up   = desc.upper()

                if "SAVINGS PLAN" in desc_up or "BUY TRADE" in desc_up:
                    tipo      = "investimento"
                    categoria = "Investimento"
                    importo   = -(abs(in_uscita or 0))
                elif "SELL TRADE" in desc_up:
                    tipo      = "investimento"
                    categoria = "Investimento"
                    importo   = abs(in_entrata or 0)
                elif "Bonifico" in tipo_text and in_entrata:
                    tipo    = "entrata"
                    importo = abs(in_entrata)
                elif in_entrata and not in_uscita:
                    tipo    = "entrata"
                    importo = abs(in_entrata)
                elif in_uscita:
                    tipo    = "spesa"
                    importo = -abs(in_uscita)
                else:
                    continue  # no amount found

                # --- Causale ---
                if "con carta" in tipo_text.lower() or tipo_text == "Transazione":
                    causale = "Transazione con carta"
                elif tipo_text:
                    causale = tipo_text
                else:
                    causale = "Commercio"

                rows.append({
                    "data":        date_str,
                    "causale":     causale,
                    "descrizione": desc or tipo_text,
                    "importo":     round(importo, 2),
                    "categoria":   categoria,
                    "banca":       banca,
                    "tipo":        tipo,
                })

    reconciliation = _check_saldo_continuity(recon_blocks)
    return rows, reconciliation


# ─── BANCA GENERALI EXCEL ─────────────────────────────────────────────────────

def _detect_header_row(df_raw):
    date_pat = re.compile(r"data|date|dat|valuta|movimento", re.I)
    desc_pat = re.compile(r"descri|operazion|causale|nota", re.I)
    amt_pat  = re.compile(r"dare|avere|importo|amount|debit|credit|uscita|entrata|saldo", re.I)
    for i, row in df_raw.iterrows():
        vals = [str(v) for v in row.values if v is not None and str(v).strip() and str(v) != "nan"]
        has_date   = any(date_pat.search(v) for v in vals)
        has_desc   = any(desc_pat.search(v) for v in vals)
        has_amount = any(amt_pat.search(v)  for v in vals)
        if has_date and (has_desc or has_amount):
            return i
    return 0


def _map_columns(headers):
    mapping = {}
    for j, h in enumerate(headers):
        h_u = str(h).upper().strip()
        if not h_u or h_u == "NAN":
            continue
        if re.search(r"\bDATA\b|DATA MOV|DATA OPE|DATA VAL", h_u) and "data" not in mapping:
            mapping["data"] = j
        elif re.search(r"\bCAUSALE\b", h_u) and "causale" not in mapping:
            mapping["causale"] = j
        elif re.search(r"DESCRI|OPERAZION", h_u) and "descrizione" not in mapping:
            mapping["descrizione"] = j
        elif re.search(r"\bDARE\b|ADDEBITO|DEBIT", h_u) and "dare" not in mapping:
            mapping["dare"] = j
        elif re.search(r"\bAVERE\b|ACCREDITO|CREDIT", h_u) and "avere" not in mapping:
            mapping["avere"] = j
        elif re.search(r"\bIMPORTO\b|AMOUNT", h_u) and "importo" not in mapping:
            mapping["importo"] = j
    return mapping


def parse_banca_generali_excel(file_bytes, banca="Banca Generali"):
    rows = []
    try:
        df_raw = pd.read_excel(io.BytesIO(file_bytes), header=None, engine="openpyxl", dtype=str)
    except Exception:
        try:
            df_raw = pd.read_excel(io.BytesIO(file_bytes), header=None, engine="xlrd", dtype=str)
        except Exception as e:
            raise ValueError(f"Impossibile leggere il file Excel: {e}")

    header_row_idx = _detect_header_row(df_raw)
    headers = list(df_raw.iloc[header_row_idx].values)
    col_map = _map_columns(headers)
    df = df_raw.iloc[header_row_idx + 1:].reset_index(drop=True)

    def get_cell(row, key):
        idx = col_map.get(key)
        if idx is None or idx >= len(row):
            return None
        v = row.iloc[idx]
        if v is None or str(v).strip() in ("", "nan", "NaN", "None"):
            return None
        return str(v).strip()

    for _, row in df.iterrows():
        data = _parse_date(get_cell(row, "data"))
        if not data:
            continue
        descrizione = get_cell(row, "descrizione") or ""
        if not descrizione:
            continue
        causale = get_cell(row, "causale") or ""

        dare        = _clean_amount(get_cell(row, "dare"))
        avere       = _clean_amount(get_cell(row, "avere"))
        importo_raw = _clean_amount(get_cell(row, "importo"))

        if importo_raw is not None:
            importo = importo_raw
            tipo = "entrata" if importo_raw > 0 else "spesa"
        elif avere is not None and dare is not None:
            importo = abs(avere) if abs(avere) > 0 else -abs(dare)
            tipo = "entrata" if importo > 0 else "spesa"
        elif avere is not None:
            importo = abs(avere)
            tipo = "entrata"
        elif dare is not None:
            importo = -abs(dare)
            tipo = "spesa"
        else:
            continue

        rows.append({
            "data": data, "causale": causale, "descrizione": descrizione,
            "importo": round(importo, 2), "categoria": "?", "banca": banca,
            "tipo": tipo,
        })
    return rows


# ─── PAYPAL CSV/EXCEL ─────────────────────────────────────────────────────────

def parse_paypal(file_bytes, banca="PayPal", existing_transactions=None):
    df_raw = None
    for engine in ("openpyxl", "xlrd"):
        try:
            df_raw = pd.read_excel(io.BytesIO(file_bytes), header=None, engine=engine, dtype=str)
            break
        except Exception:
            pass
    if df_raw is None:
        try:
            df_raw = pd.read_csv(io.BytesIO(file_bytes), header=None, dtype=str)
        except Exception:
            return []

    header_row_idx = _detect_header_row(df_raw)
    headers = list(df_raw.iloc[header_row_idx].values)
    col_map = _map_columns(headers)
    df = df_raw.iloc[header_row_idx + 1:].reset_index(drop=True)

    rows = []
    for _, row in df.iterrows():
        def get_cell(key, _row=row, _col=col_map):
            idx = _col.get(key)
            if idx is None or idx >= len(_row):
                return None
            v = _row.iloc[idx]
            if v is None or str(v).strip() in ("", "nan", "NaN", "None"):
                return None
            return str(v).strip()

        data = _parse_date(get_cell("data"))
        if not data:
            continue
        descrizione = get_cell("descrizione") or ""
        dare        = _clean_amount(get_cell("dare"))
        avere       = _clean_amount(get_cell("avere"))
        importo_raw = _clean_amount(get_cell("importo"))

        if importo_raw is not None:
            importo = importo_raw
            tipo = "entrata" if importo_raw > 0 else "spesa"
        elif avere is not None:
            importo = abs(avere)
            tipo = "entrata"
        elif dare is not None:
            importo = -abs(dare)
            tipo = "spesa"
        else:
            continue

        is_duplicate = False
        if existing_transactions:
            from datetime import datetime as dt
            try:
                tx_date = dt.strptime(data, "%Y-%m-%d")
            except Exception:
                tx_date = None
            for ex in existing_transactions:
                try:
                    ex_date = dt.strptime(str(ex.get("data", "")), "%Y-%m-%d")
                except Exception:
                    continue
                if tx_date and abs((tx_date - ex_date).days) <= 2 and abs(float(ex.get("importo", 0)) - abs(importo)) < 0.02:
                    is_duplicate = True
                    break

        rows.append({
            "data": data, "causale": "PayPal", "descrizione": descrizione,
            "importo": round(importo, 2), "categoria": "?", "banca": banca,
            "tipo": tipo, "_duplicate": is_duplicate,
        })
    return rows


# ─── TRADE REPUBLIC CSV (nuovo export operazioni) ─────────────────────────────
#
# Trade Republic ora esporta le operazioni come CSV con colonne già strutturate
# (date, amount, fee, tax, type, asset_class, name, symbol/ISIN, description,
#  transaction_id, counterparty_name, mcc_code, …). I dati si leggono diretti:
# nessuna AI serve per ESTRARLI — l'AI resta solo per interpretare descrizione e
# categoria, esattamente come per Banca Generali.
#
# importo = amount + fee + tax  →  impatto netto reale sul conto
#   (fee e tax sono già negativi quando presenti: SELL / DIVIDEND / interessi /
#    bollo). Per le normali transazioni con carta fee e tax sono vuoti.

# type → (tipo, causale base). La categoria fine la assegna poi l'AI o l'utente.
TR_CSV_TYPE_MAP = {
    "CARD_TRANSACTION":               ("spesa",        "Carta"),
    "CARD_TRANSACTION_INTERNATIONAL": ("spesa",        "Carta"),
    "BUY":                            ("investimento", "Investimento"),
    "SELL":                           ("investimento", "Investimento"),
    "DIVIDEND":                       ("entrata",      "Altro"),
    "INTEREST_PAYMENT":               ("entrata",      "Altro"),
    "BENEFITS_SAVEBACK":              ("entrata",      "Altro"),
    "TAX_OPTIMIZATION":               ("spesa",        "Altro"),
    "TRANSFER_INSTANT_INBOUND":       ("entrata",      "Bonifico"),
    "TRANSFER_INSTANT_OUTBOUND":      ("spesa",        "Bonifico"),
}


def _strip_fx_noise(desc):
    """Toglie la coda 'valuta estera' dalle descrizioni carta
    (es. '…, 2,40 $, exchange rate: …, markup: …') e gli IBAN tra parentesi,
    lasciando il nome del commerciante / la frase utile per l'AI."""
    if not desc:
        return ""
    d = re.sub(r",?\s*exchange\s+rate:.*", "", desc, flags=re.I).strip()
    d = re.sub(r"\s*\d[\d.,]*,?\s*markup:.*", "", d, flags=re.I).strip()
    d = re.sub(r"\s*%null\b.*", "", d, flags=re.I).strip()
    d = re.sub(r",\s*[\d.,]+\s*[A-Z]{0,3}\$\s*$", "", d).strip()
    d = re.sub(r"\s*\([A-Z]{2}\d{2}[A-Z0-9]+\)", "", d).strip()   # IBAN tra ()
    return d


def _is_trade_republic_csv(file_bytes):
    """Riconosce il CSV di Trade Republic dalla riga di intestazione."""
    try:
        head = file_bytes[:4096].decode("utf-8-sig", errors="ignore").lower()
        first_line = head.splitlines()[0] if head else ""
        return "transaction_id" in first_line and "asset_class" in first_line
    except Exception:
        return False


def parse_trade_republic_csv(file_bytes, banca="Trade Republic"):
    text = file_bytes.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))

    rows = []
    for r in reader:
        raw_date = (r.get("date") or "").strip()
        # Il CSV di TR ha la data già in ISO (YYYY-MM-DD): usala diretta.
        # NON passarla da _parse_date (è day-first, inadatto all'ISO → invertirebbe
        # giorno e mese, es. 2026-04-01 → 2026-01-04).
        if re.match(r"^\d{4}-\d{2}-\d{2}$", raw_date):
            data = raw_date
        else:
            data = _parse_date(raw_date)
        if not data:
            continue

        amount = _clean_amount(r.get("amount")) or 0.0
        fee    = _clean_amount(r.get("fee"))    or 0.0
        tax    = _clean_amount(r.get("tax"))    or 0.0
        importo = round(amount + fee + tax, 2)
        if importo == 0:
            continue   # righe senza impatto di cassa

        ttype = (r.get("type") or "").strip().upper()
        name  = (r.get("name") or "").strip()
        descr = _strip_fx_noise((r.get("description") or "").strip()) or name or ttype

        tipo, causale = TR_CSV_TYPE_MAP.get(
            ttype, ("entrata" if importo >= 0 else "spesa", "Altro")
        )
        categoria = "Investimento" if ttype in ("BUY", "SELL", "DIVIDEND") else "?"

        row = {
            "data":           data,
            "causale":        causale,
            "descrizione":    descr,
            "importo":        importo,
            "categoria":      categoria,
            "banca":          banca,
            "tipo":           tipo,
            "transaction_id": (r.get("transaction_id") or "").strip(),
        }

        # Dettaglio trade per il costo di carico — SOLO acquisti/vendite di titoli.
        # Questi campi NON entrano nel CSV cassa: vengono persistiti a parte
        # (investment_lots.json) e alimentano il motore costo medio.
        # NB: migrazioni/saveback/dividendi NON sono lotti (le migrazioni sono
        # coppie a saldo zero, i saveback sono cassa, i dividendi non muovono quote).
        if ttype in ("BUY", "SELL"):
            isin   = (r.get("symbol") or "").strip()
            shares = _clean_amount(r.get("shares"))
            price  = _clean_amount(r.get("price"))
            if isin and shares:
                row["_lot"] = {
                    "transaction_id": row["transaction_id"],
                    "date":   data,
                    "isin":   isin,
                    "name":   name,
                    "side":   ttype,
                    "shares": round(abs(shares), 8),
                    "price":  round(price, 6) if price is not None else None,
                    "gross":  round(abs(amount), 2),
                    "fee":    round(abs(fee), 2),
                    "tax":    round(abs(tax), 2),
                }

        rows.append(row)
    return rows


# ─── DISPATCHER ───────────────────────────────────────────────────────────────

def parse_file(file_bytes, filename, banca, existing_transactions=None):
    """
    Returns (rows, reconciliation) where reconciliation is a dict for PDF files
    (see _check_saldo_continuity) or None for other formats.
    """
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext == "pdf":
        return parse_trade_republic_pdf(file_bytes, banca)   # already a tuple
    elif ext == "csv" and (banca == "Trade Republic" or _is_trade_republic_csv(file_bytes)):
        return parse_trade_republic_csv(file_bytes), None
    elif banca == "PayPal" or ext == "csv":
        return parse_paypal(file_bytes, banca, existing_transactions), None
    elif ext in ("xls", "xlsx"):
        return parse_banca_generali_excel(file_bytes, banca), None
    else:
        return [], None
