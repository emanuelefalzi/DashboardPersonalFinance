"""
Motore del costo medio ponderato per gli investimenti.

A partire dai lotti (acquisti/vendite di titoli) e dalle eventuali posizioni
iniziali, calcola:
  - per ogni ISIN ancora posseduto: quote, costo di carico, prezzo medio
  - per ogni vendita: profitto realizzato (€ e %)
  - avvisi (es. vendite senza acquisto precedente → "orphan")

Convenzione (decisa con l'utente):
  - costo/proventi = importo LORDO (= shares * price); fee e tax = costi della
    transazione. La fee di acquisto si capitalizza nel costo.
  - SELL: realizzato = proventi - costo_venduto - fee - tax
  - metodo: costo medio ponderato (l'avg non cambia quando si vende).

Modulo puro: nessun I/O, nessuna rete.
"""

EPS = 1e-6


def _num(x, default=0.0):
    try:
        if x is None or x == "":
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


def build_ledger(lots, opening_positions=None):
    """
    lots: lista di dict {transaction_id, date, isin, name, side('BUY'|'SELL'),
                         shares, price, gross, fee, tax}
    opening_positions: lista di dict {isin, name, shares, cost, as_of}

    Ritorna: {
      "positions": [ {isin, name, shares, cost_basis, avg_cost} ]  # solo aperte
      "sales":     [ {transaction_id, isin, name, date, shares_sold, proceeds,
                      cost_removed, realized, realized_pct, orphan} ]
      "by_isin":   { isin: {name, shares, cost_basis, avg_cost, realized_total} }
      "warnings":  [str]
    }
    """
    opening_positions = opening_positions or []
    state = {}        # isin -> {name, shares, cost}
    realized_by = {}  # isin -> realized_total
    sales = []
    warnings = []

    def st(isin, name=""):
        if isin not in state:
            state[isin] = {"name": name or isin, "shares": 0.0, "cost": 0.0}
            realized_by[isin] = 0.0
        if name and state[isin]["name"] in ("", isin):
            state[isin]["name"] = name
        return state[isin]

    # 1) seed posizioni iniziali (titoli posseduti prima dei lotti disponibili)
    for op in opening_positions:
        isin = (op.get("isin") or "").strip()
        if not isin:
            continue
        s = st(isin, op.get("name", ""))
        s["shares"] += _num(op.get("shares"))
        s["cost"]   += _num(op.get("cost"))

    # 2) replay dei lotti in ordine di data (poi transaction_id per stabilità)
    ordered = sorted(lots, key=lambda l: (str(l.get("date", "")),
                                          0 if (l.get("side") or "").upper() == "BUY" else 1,
                                          str(l.get("transaction_id", ""))))
    for lot in ordered:
        isin = (lot.get("isin") or "").strip()
        if not isin:
            continue
        side   = (lot.get("side") or "").upper()
        shares = abs(_num(lot.get("shares")))
        if lot.get("gross") is not None:
            gross = abs(_num(lot.get("gross")))
        else:
            gross = abs(_num(lot.get("price")) * shares)
        fee = abs(_num(lot.get("fee")))
        tax = abs(_num(lot.get("tax")))
        s = st(isin, lot.get("name", ""))

        if side == "BUY":
            s["shares"] += shares
            s["cost"]   += gross + fee

        elif side == "SELL":
            if s["shares"] <= EPS:
                warnings.append(
                    f"Vendita senza carico per {isin} il {lot.get('date')}: "
                    f"{shares:g} quote (manca l'acquisto o la posizione iniziale)."
                )
                sales.append({
                    "transaction_id": lot.get("transaction_id", ""),
                    "isin": isin, "name": s["name"], "date": lot.get("date", ""),
                    "shares_sold": round(shares, 8), "proceeds": round(gross, 2),
                    "cost_removed": None, "realized": None, "realized_pct": None,
                    "orphan": True,
                })
                continue
            avg          = s["cost"] / s["shares"] if s["shares"] else 0.0
            sell_shares  = min(shares, s["shares"])
            # Se si vende più di quanto a carico, conta solo la parte coperta
            # (proventi e costi pro-quota) e segnala l'eccesso.
            ratio        = (sell_shares / shares) if shares > EPS else 1.0
            if ratio < 1.0:
                warnings.append(
                    f"Vendita di {shares:g} quote ma solo {sell_shares:g} a carico per {isin} "
                    f"il {lot.get('date')}: l'eccesso è stato ignorato.")
            proceeds     = gross * ratio
            cost_removed = avg * sell_shares
            realized     = proceeds - cost_removed - fee * ratio - tax * ratio
            realized_pct = (realized / cost_removed * 100) if cost_removed > EPS else None
            s["shares"] -= sell_shares
            s["cost"]   -= cost_removed
            if abs(s["shares"]) < EPS:
                s["shares"] = 0.0
                s["cost"]   = 0.0
            realized_by[isin] += realized
            sales.append({
                "transaction_id": lot.get("transaction_id", ""),
                "isin": isin, "name": s["name"], "date": lot.get("date", ""),
                "shares_sold": round(sell_shares, 8), "proceeds": round(proceeds, 2),
                "cost_removed": round(cost_removed, 2), "realized": round(realized, 2),
                "realized_pct": round(realized_pct, 2) if realized_pct is not None else None,
                "orphan": False,
            })

    # 3) finalizza
    positions = []
    by_isin = {}
    for isin, s in state.items():
        shares = s["shares"]
        cost   = s["cost"]
        avg    = cost / shares if shares > EPS else 0.0
        by_isin[isin] = {
            "name": s["name"], "shares": round(shares, 8),
            "cost_basis": round(cost, 2), "avg_cost": round(avg, 6),
            "realized_total": round(realized_by.get(isin, 0.0), 2),
        }
        if shares > EPS:
            positions.append({
                "isin": isin, "name": s["name"], "shares": round(shares, 8),
                "cost_basis": round(cost, 2), "avg_cost": round(avg, 6),
            })

    positions.sort(key=lambda p: -p["cost_basis"])
    return {"positions": positions, "sales": sales,
            "by_isin": by_isin, "warnings": warnings}
