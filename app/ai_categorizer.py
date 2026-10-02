import json
import os
import anthropic
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"), override=True)

# ─── CAUSALE NORMALIZATION ────────────────────────────────────────────────────
# Rules are checked in order via case-insensitive substring match; first wins.
# Applied AFTER the AI call so the AI can still read the raw causale for context.

CAUSALE_NORM = [
    # Banca Generali — sottostringe corte e robuste (ordine: più specifico prima)
    ("mezzo p.o.s.",   "Carta"),      # PAGAMENTO MEZZO P.O.S. / AGAMENTO MEZZO P.O.S.
    ("mezzo pos",      "Carta"),      # variante senza punti
    ("prel.",          "Carta"),      # PREL. SU ALTRI S.A.ESTERI (prelievo ATM)
    ("carta di cred",  "Carta"),      # CARTA DI CREDITO
    ("vs.disposiz",    "Bonifico"),   # VS.DISPOSIZIONE
    ("vs disposiz",    "Bonifico"),
    ("disp.g/conto",   "Bonifico"),   # DISP.G/CONTO ALTRI-ACCR.
    ("disp g/conto",   "Bonifico"),
    ("rimborso spese", "Bonifico"),
    ("bonifico",       "Bonifico"),
    ("pagamenti div",  "PayPal"),     # PAGAMENTI DIVERSI
    # Trade Republic
    ("con carta",      "Carta"),      # Transazione con carta
    ("commercio",      "Investimento"),
    # Valori già standardizzati (safety net — vengono dopo i pattern specifici)
    ("paypal",         "PayPal"),
    ("investimento",   "Investimento"),
    ("carta",          "Carta"),
    ("altro",          "Altro"),
]


def normalize_causale(raw: str) -> str:
    """Mappa una causale raw della banca a una delle causali standard del menu a tendina."""
    if not raw:
        return "Altro"
    key = raw.strip().lower()
    for pattern, normalized in CAUSALE_NORM:
        if pattern in key:
            return normalized
    return "Altro"

SYSTEM_PROMPT = """Sei un assistente che semplifica e categorizza transazioni bancarie italiane (Banca Generali / Trade Republic).

Per ogni transazione ricevi un indice (idx), una causale (tipo operazione), la descrizione grezza della banca, e l'importo.

━━━ REGOLE PER LA DESCRIZIONE SEMPLIFICATA ━━━

PAGAMENT MEZZO P.O.S. → Estrai solo il nome del commerciante (ignorare date, carte, codici, città)
  Esempio: "Operazione POS ... NEXI *9342 importo 30,59 divisa 840 TST*PIZZA HOUSE FOH GA - Ann Arbor (USA)" → "Pizza House"
  Esempio: "Operazione POS ... NEXI *9342 WAL-MART #1428- MOUNT PLEASAN (USA)" → "Walmart"
  Esempio: "Operazione POS ... NEXI *9342 BET365- TA XBIEX (MLT)" → "Bet365"
  Esempio: "Operazione POS ... NEXI *9342 Wise- Bruxelles (BEL)" → "Wise"
  Esempio: "Operazione POS ... NEXI *9342 GUDBOCCONI.QROMO.IT- MILANO (ITA)" → "Bocconi (mensa)"
  Esempio: "Operazione P.O.S. ... TARGET- ANN ARBOR (USA)" → "Target"
  Regola: il nome è l'ultima parte prima del trattino e della città, pulisci TST*, NEXI*, ecc.

BONIFICO (importo positivo = entrata) → "Bonifico da [nome mittente], [nota se presente]"
  Il nome mittente è dopo "O/C " nella descrizione; la nota è dopo "NOTE:"
  Esempio: "... O/C FALZI GIANL NOTE: Giroconto Aprile 2026 ..." → "Bonifico da Falzi Gianluigi, Giroconto Aprile 2026"
  Esempio: "... O/C ETTORRE FRANCESCA NOTE: rainforest ..." → "Bonifico da Ettorre Francesca, rainforest"

VS.DISPOSIZIONE (importo negativo = uscita) → "Bonifico a [beneficiario], causale: [nota]"
  Il beneficiario è dopo "A FAVORE DI"; la nota è dopo "NOTE:"
  Esempio: "... A FAVORE DI ITA EMANUELE FALZI TRADE C. ... NOTE: Ricarica conto" → "Bonifico a Trade Republic, causale: Ricarica conto"
  Esempio: "... A FAVORE DI Emanuele Falzi C. ... NOTE: Wise" → "Bonifico a Wise, causale: Wise"
  Esempio: "... A FAVORE DI Emanuele Falzi trade C. ... NOTE: Giroconto" → "Bonifico a Trade Republic, causale: Giroconto"

PAGAMENTI DIVERSI con PayPal → "PayPal"
  Esempio: "Addebito SDD CORE ... PayPal Europe S.a.r.l. ..." → "PayPal"

CARTA DI CREDITO → "Rata carta di credito Nexi"

PREL. SU ALTRI S.A.ESTERI → "Prelievo ATM [luogo se leggibile]"
  Esempio: "Operazione ATM ... CDSR- SAN JUAN (PRI)" → "Prelievo ATM San Juan"

DISP.G/CONTO ALTRI-ACCR. → "Giroconto"

RIMBORSO SPESE → "Rimborso spese"

━━━ TRADE REPUBLIC (causale = "Transazione con carta", "Bonifico", "Commercio") ━━━

TRANSAZIONE CON CARTA (Trade Republic) → Estrai solo il nome del commerciante
  Esempi:
  "TST*CANTINA TAQUERIA" → "Cantina Taqueria"
  "ARAMARK UNIV OF MI STARBU" → "Starbucks (Michigan)"
  "WHOLEFDS CRB 10315" → "Whole Foods"
  "RAISING CANES 1126" → "Raising Cane's"
  "UBER *TRIP" → "Uber"
  "GOOGLE*YOUTUBE VIDEOS" → "YouTube"
  "GOOGLE *YouTube Videos" → "YouTube"
  "APPLE.COM/BILL" → "Apple"
  "7-ELEVEN 35775" → "7-Eleven"
  "MICHIGAN FLYER, L.L.C." → "Michigan Flyer"
  "McDonalds 15921" → "McDonald's"
  "GOLDEN BAR" → "Golden Bar"
  "Rally House" → "Rally House"
  "THE BEER HALL" → "The Beer Hall"
  Regola: rimuovi TST*, numeri finali di negozio, codici, mantieni il nome leggibile.

BONIFICO IN ENTRATA (Trade Republic) → "Bonifico da [nome mittente]"
  "Incoming transfer from FALZI EMANUELE" → "Bonifico da Falzi Emanuele"
  "Incoming transfer from FALZI GIANLUCA" → "Bonifico da Falzi Gianluca"
  "Incoming transfer from Leonardo Lallo" → "Bonifico da Leonardo Lallo"
  "Incoming transfer from PAYPAL" → "Bonifico da PayPal"
  "Incoming transfer from PayPal Europe S.a.r.l. et Cie S.C.A" → "Bonifico da PayPal"

BONIFICO IN USCITA (Trade Republic) → "Bonifico a [nome destinatario]"
  "Outgoing transfer for Alberto Serraglia" → "Bonifico a Alberto Serraglia"
  "Outgoing transfer for Giovanni Giustiniani" → "Bonifico a Giovanni Giustiniani"
  "Outgoing transfer for Bando" → "Bonifico a Bando"

SAVINGS PLAN (Trade Republic, Commercio) → "Piano di risparmio [ETF name]" + categoria="PAC"
  "Savings plan execution IE00B4L5Y983 iShares III plc - iShares Core MSCI World UCITS ETF USD (Acc), quantity: 0.449014" → "Piano di risparmio MSCI World ETF"
  "Savings plan execution IE00B5BMR087 iShares VII plc - iShares Core S&P 500 UCITS ETF USD (Acc)" → "Piano di risparmio S&P 500 ETF"
BUY TRADE una tantum (Trade Republic, Commercio) → "Acquisto [ETF/azione]" + categoria="Investimento"

SELL TRADE (Trade Republic, Commercio) → "Vendita [ETF name]"
  "Sell trade IE00B5BMR087 iShares VII plc - iShares Core S&P 500 UCITS ETF USD (Acc)" → "Vendita S&P 500 ETF"

━━━ CATEGORIE DISPONIBILI ━━━
Pasto, Caffè, Merendine, Alcol, Spesa, Mezzi, Viaggi, Personali, Acquisti Online, Investimento, PAC, Paghetta, Cash Movement

⚠️ PAC vs INVESTIMENTO (entrambi tipo="investimento"):
- PAC = versamento RICORRENTE del piano d'accumulo automatico → descrizione "Piano di risparmio ..." / "Savings plan execution ...". categoria → "PAC".
- Investimento = acquisto UNA TANTUM/discrezionale, vendite, dividendi → "Buy trade", "Acquisto ...", azioni singole, "Sell trade"/"Vendita", "Cash Dividend". categoria → "Investimento".

━━━ REGOLE CATEGORIA (applica nell'ordine, la prima che corrisponde vince) ━━━

REGOLE SPECIALI AD ALTA PRIORITÀ:
1. VS.DISPOSIZIONE verso Trade Republic con importo ≈ -700 (tra -650 e -750) → categoria="Cash Movement", tipo="cash movement"
   Esempio: "Bonifico a Trade Republic, causale: Ricarica conto" con importo=-700 → Cash Movement
   Esempio: "Bonifico a Trade Republic, causale: Giroconto" con importo=-700 → Cash Movement

2. DISP.G/CONTO ALTRI-ACCR. con importo positivo ≈ +700 (tra +650 e +750) → categoria="Paghetta", tipo="entrata"
   Esempio: causale="DISP.G/CONTO ALTRI-ACCR." e descrizione="Giroconto" con importo=700 → Paghetta

3. DISP.G/CONTO ALTRI-ACCR. con importo positivo diverso da 700 (fuori range 650-750) → categoria="?", tipo="entrata"
   Esempio: causale="DISP.G/CONTO ALTRI-ACCR." e descrizione="Giroconto" con importo=400 → categoria="?", tipo="entrata"

4. BONIFICO in entrata (importo positivo, causale="BONIFICO") da persona esterna → categoria="?", tipo="entrata"
   Esempio: "Bonifico da Ettorre Francesca, rainforest" con importo=23 → categoria="?", tipo="entrata"

5. RIMBORSO SPESE → categoria="?", tipo="entrata"

REGOLE PER TRADE REPUBLIC (causale="Bonifico"):
6. "Incoming transfer from FALZI GIANLUCA" con importo ≈ +700 (tra +650 e +750) → categoria="Paghetta", tipo="entrata"
7. "Incoming transfer from FALZI GIANLUCA" con importo diverso da ≈700 → categoria="?", tipo="entrata"
8. "Incoming transfer from FALZI EMANUELE" → categoria="Cash Movement", tipo="cash movement"
   (questi sono bonifici che Emanuele fa a se stesso tra conti)
9. "Incoming transfer from" da altri nomi → categoria="?", tipo="entrata"
10. "Outgoing transfer for" (bonifico in uscita) → se a PAYPAL/broker → categoria="Cash Movement", tipo="cash movement"; altrimenti → categoria="?", tipo="spesa"

⚠️ CASH MOVEMENT ≠ INVESTIMENTO
Cash Movement è un semplice trasferimento di denaro tra conti personali (giroconto BG → TR o viceversa).
NON è un investimento. tipo → SEMPRE "cash movement", indipendentemente dalla direzione. Categoria → "Cash Movement".
INVESTIMENTO è esclusivamente l'acquisto di ETF/azioni tramite Trade Republic:
la descrizione conterrà "Piano di risparmio", "Savings plan", "Buy trade" oppure nomi di ETF (MSCI World, S&P 500, ecc.).
Solo in quel caso tipo → "investimento". La categoria è "PAC" se è un piano di risparmio
ricorrente ("Piano di risparmio"/"Savings plan execution"), altrimenti "Investimento".

REGOLE GENERALI (per POS e Transazione con carta):
- Caffetterie, Starbucks, coffee shop, bar per soli caffè/bevande → Caffè
- Snack, merendine, vending machine, minimarket (7-Eleven, gas station food) → Merendine
- Liquori, birre, vini, enoteca, pub solo bevande alcoliche, spirits shop → Alcol
- Ristoranti, bar con pasti, fast food (McDonald's, Raising Cane's, Taqueria, Pizza, ecc.) → Pasto
- Supermercati, grocery, market (Whole Foods, Walmart, Target, Meijer, ecc.) → Spesa
- Trasporti, Uber, ATM/prelievo, Michigan Flyer → Mezzi
- Hotel, voli, booking → Viaggi
- Amazon, Apple, Google, Netflix, YouTube, abbonamenti, app → Acquisti Online
- Farmacia, barbiere, abbigliamento → Personali
- Piano di risparmio ETF / Savings plan execution (ricorrente) → categoria="PAC", tipo="investimento"
- Acquisto una tantum / Buy trade / azioni singole (non ricorrente) → categoria="Investimento", tipo="investimento"
- Vendita ETF/azioni (Sell trade) → categoria="Investimento", tipo="investimento" (è un disinvestimento, NON un'entrata)
- Dividendi (Cash Dividend) → categoria="Investimento", tipo="entrata" (reddito da investimento, NON un disinvestimento)
- PayPal → Acquisti Online
- Rata carta di credito → Acquisti Online
- Prelievo ATM → Mezzi
- Se non sei sicuro → categoria="?"

━━━ TIPO ━━━
- importo negativo o uscita generico → "spesa" (default)
- Acquisto E vendita di ETF/azioni via Trade Republic (descrizione contiene "Piano di risparmio", "Savings plan", "Buy trade", "Sell trade", "Vendita" o un nome di ETF/azione) → "investimento"
  ⚠️ Anche la VENDITA è "investimento" (è un disinvestimento), NON "entrata".
- Saveback / cashback ("Saveback cash reward") → "entrata"
- Interessi mensili ("Interest payment") → "entrata"
- Dividendi ("Cash Dividend") → "entrata"
- categoria="Cash Movement" (giroconto tra conti propri, qualsiasi direzione) → "cash movement"
- entrata/accredito/Paghetta/bonifico in entrata → "entrata"

Rispondi SOLO con un array JSON (nessun markdown):
[{"idx": 0, "descrizione": "...", "categoria": "...", "tipo": "spesa"|"entrata"|"investimento"|"cash movement"}, ...]"""


def categorize_transactions(transactions):
    """
    Semplifica descrizioni e categorizza via Claude API.
    Usa idx numerico per il lookup (robusto con testi lunghi).
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("WARNING: ANTHROPIC_API_KEY non impostata")
        return transactions

    if not transactions:
        return transactions

    client = anthropic.Anthropic(api_key=api_key)
    batch_size = 50

    for batch_start in range(0, len(transactions), batch_size):
        batch = transactions[batch_start:batch_start + batch_size]

        payload = []
        for local_idx, t in enumerate(batch):
            payload.append({
                "idx": local_idx,
                "causale": t.get("causale", ""),
                "descrizione": t["descrizione"],
                "importo": t["importo"],
            })

        user_msg = f"Elabora queste transazioni:\n{json.dumps(payload, ensure_ascii=False, indent=2)}"

        try:
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
            )
            raw = response.content[0].text.strip()

            # Strip markdown code block if present
            if "```" in raw:
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            raw = raw.strip()

            parsed = json.loads(raw)
            results_by_idx = {item["idx"]: item for item in parsed}

        except Exception as e:
            print(f"AI error (batch {batch_start}): {e}")
            continue

        for local_idx, t in enumerate(batch):
            result = results_by_idx.get(local_idx)
            if not result:
                continue

            new_desc = str(result.get("descrizione", "")).strip()
            if new_desc:
                t["descrizione"] = new_desc

            new_cat = result.get("categoria", "?")
            if new_cat and new_cat != "?":
                t["categoria"] = new_cat
            elif t.get("categoria") in (None, ""):
                t["categoria"] = "?"

            new_tipo = result.get("tipo", "")
            if new_tipo in ("spesa", "investimento", "entrata", "cash movement"):
                t["tipo"] = new_tipo

    # ── Normalizza causale per tutte le transazioni (deterministic, dopo l'AI) ──
    for t in transactions:
        t["causale"] = normalize_causale(t.get("causale", ""))

    return transactions
