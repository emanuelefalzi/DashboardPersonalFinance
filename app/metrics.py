"""
Metriche di portafoglio (matematica pura, no I/O, no rete — testabile).

⚠️ Nota critica: investments.json salva il VALORE della posizione per mese, non
il prezzo unitario, ed è gonfiato dai versamenti (un acquisto fa salire il valore
senza essere un guadagno). Per volatilità/rendimento si ricostruisce il PREZZO
UNITARIO = valore[m] / quote_possedute_a_fine_m (quote ricostruite dai lotti).
"""

import math
import statistics


# ── Ricostruzione quote dai lotti ─────────────────────────────────────────────

def _isin_lots(lots, isin):
    return sorted((l for l in lots if l.get("isin") == isin),
                  key=lambda l: str(l.get("date", "")))


def shares_at_month_end(lots, isin, month):
    """Quote cumulate (BUY +, SELL −) fino a fine `month` (YYYY-MM)."""
    sh = 0.0
    for l in _isin_lots(lots, isin):
        if str(l.get("date", ""))[:7] <= month:
            q = abs(float(l.get("shares") or 0))
            sh += q if (l.get("side", "").upper() == "BUY") else -q
    return sh


def unit_price_series(values, lots, isin):
    """values={mese:valore_mercato} → {mese:prezzo_unitario} = valore / quote a fine mese."""
    out = {}
    for m in sorted(values):
        v = float(values[m] or 0)
        sh = shares_at_month_end(lots, isin, m)
        if sh > 1e-9 and v > 0:
            out[m] = v / sh
    return out


# ── Rendimenti e volatilità ───────────────────────────────────────────────────

def monthly_returns(price_series):
    months = sorted(price_series)
    rets = []
    for i in range(1, len(months)):
        p0, p1 = price_series[months[i - 1]], price_series[months[i]]
        if p0 and p0 > 0:
            rets.append(p1 / p0 - 1)
    return rets


def annualized_vol(returns):
    """Dev. standard campionaria dei rendimenti mensili × √12. None se < 2 rendimenti."""
    if not returns or len(returns) < 2:
        return None
    return statistics.stdev(returns) * math.sqrt(12)


def portfolio_monthly_returns(per_isin_prices, weights):
    """Serie di rendimenti del portafoglio = media pesata (pesi costanti) dei rendimenti
    per ISIN mese per mese. per_isin_prices = {isin: {mese: prezzo}}; weights = {isin: peso}."""
    months = sorted({m for ps in per_isin_prices.values() for m in ps})
    tot_w = sum(weights.values()) or 1.0
    out = []
    for i in range(1, len(months)):
        m0, m1 = months[i - 1], months[i]
        acc, wsum = 0.0, 0.0
        for isin, ps in per_isin_prices.items():
            if m0 in ps and m1 in ps and ps[m0] > 0:
                w = weights.get(isin, 0) / tot_w
                acc += w * (ps[m1] / ps[m0] - 1)
                wsum += w
        if wsum > 0:
            out.append(acc / wsum)
    return out


# ── Diversificazione ──────────────────────────────────────────────────────────

def hhi(weights):
    """Herfindahl-Hirschman su pesi (normalizzati). ∈ (0,1]; alto = concentrato."""
    s = sum(w for w in weights if w > 0)
    if s <= 0:
        return None
    return sum((w / s) ** 2 for w in weights if w > 0)


def effective_n(weights):
    """Numero di titoli 'effettivi' = 1/HHI."""
    h = hhi(weights)
    return (1.0 / h) if h else None


def diversification_band(h):
    if h is None:
        return "—"
    if h < 0.15:
        return "ben diversificato"
    if h < 0.25:
        return "moderatamente concentrato"
    return "concentrato"


# ── Rischio (SRRI ESMA → 4 livelli) ───────────────────────────────────────────

# (limite superiore di volatilità annualizzata, classe SRRI)
_SRRI_BANDS = [(0.005, 1), (0.02, 2), (0.05, 3), (0.10, 4), (0.15, 5), (0.25, 6)]


def srri_from_vol(vol):
    if vol is None:
        return None
    for upper, srri in _SRRI_BANDS:
        if vol < upper:
            return srri
    return 7


def srri_to_bucket(srri):
    if srri is None:
        return None
    if srri <= 2:
        return "Basso"
    if srri <= 4:
        return "Medio-basso"
    if srri == 5:
        return "Medio-alto"
    return "Alto"


def assetclass_risk_fallback(entity_type):
    """Stima del rischio per classe quando manca la volatilità."""
    t = (entity_type or "").upper()
    if t == "CASH":
        return "Basso"
    if t == "ETF":
        return "Medio-alto"
    return "Alto"   # azione singola


RISK_ORDER = ["Basso", "Medio-basso", "Medio-alto", "Alto"]


# ── Patrimonio nel tempo ──────────────────────────────────────────────────────

def networth_series(cash_by_month, invest_by_month):
    """[{mese, cash, investiti, totale}] per tutti i mesi presenti, ordine crescente."""
    months = sorted(set(cash_by_month) | set(invest_by_month))
    out = []
    for m in months:
        cash = round(float(cash_by_month.get(m, 0) or 0), 2)
        inv = round(float(invest_by_month.get(m, 0) or 0), 2)
        out.append({"month": m, "cash": cash, "investiti": inv, "totale": round(cash + inv, 2)})
    return out
