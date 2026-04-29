import io
import json
import os
import pdfplumber
import anthropic
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"), override=True)

TARGET_ACCOUNT = "0716599502"

# ── AI PROMPTS ────────────────────────────────────────────────────────────────

EXTRACT_PROMPT = """Sei un assistente che analizza estratti conto titoli di Trade Republic Bank Italy.

Ricevi il testo estratto da un PDF. Trova la sezione relativa al conto titoli 0716599502
(la pagina/sezione con "CONTO TITOLI 0716599502").

Da essa estrai:
1. La data di riferimento: è scritta sotto "ESTRATTO CONTO TITOLI" come "al GG.MM.AAAA".
   Convertila in formato "YYYY-MM" (es. "al 31.01.2026" → "2026-01").
2. Per ogni posizione che ha un ISIN:
   - isin: il codice ISIN (es. "IE00B4L5Y983")
   - nome_raw: il nome completo del titolo — concatena la prima riga del nome (quella in grassetto)
     e la seconda riga di descrizione (es. "Registered Shs USD (Acc) o.N.") con uno spazio
   - valore: il "Valore di Mercato in EUR" come numero float.
     Converti il formato italiano: "3.670,97" → 3670.97, "46,71" → 46.71

Rispondi SOLO con JSON valido, senza markdown, senza spiegazioni:
{"month": "2026-01", "positions": [{"isin": "IE00B4L5Y983", "nome_raw": "iShsIII-Core MSCI World U.ETF Registered Shs USD (Acc) o.N.", "valore": 3670.97}]}

Se il conto 0716599502 non ha posizioni (NUMERO DI POSIZIONI: 0), rispondi:
{"month": "YYYY-MM", "positions": []}"""

SIMPLIFY_PROMPT = """Sei un assistente che semplifica i nomi di strumenti finanziari in nomi brevi, leggibili e immediatamente riconoscibili.

Regole di semplificazione:
- ETF iShares basati su MSCI World (qualsiasi variante) → "MSCI World ETF"
- ETF iShares basati su S&P 500 (qualsiasi variante) → "S&P 500 ETF"
- ETF iShares MSCI Emerging Markets → "Emerging Markets ETF"
- Azioni di aziende note: usa il nome brand comune (es. BYD, Meta, PayPal, Apple, Tesla, Intesa Sanpaolo, Nvidia, Amazon, ecc.)
- Rimuovi completamente: codici tecnici, classi di azioni (Cl.A, o.N., YC 1, DL-xxx), "Registered", "Reg.", sigle borsistiche, indicatori di valuta (USD Acc, EUR)
- Massimo 3-4 parole per nome

Ricevi un array JSON con {isin, nome_raw}. Rispondi SOLO con JSON array, senza markdown:
[{"isin": "...", "name": "..."}]"""


# ── TYPE DETECTION (deterministic, no AI) ─────────────────────────────────────

def detect_type(raw_name: str) -> str:
    """
    Deduce type from raw name text:
    - ETF: contains 'ETF'
    - S (stock): contains 'Azioni', 'Reg.Shares', 'Reg. Shares', or 'Registered Shares'
    - ?: unknown (user fills in)
    """
    u = raw_name.upper()
    if "ETF" in u:
        return "ETF"
    if "AZIONI" in u:
        return "S"
    if "REG.SHARES" in u or "REG. SHARES" in u or "REGISTERED SHARES" in u:
        return "S"
    return "?"


# ── PDF TEXT EXTRACTION ───────────────────────────────────────────────────────

def extract_pdf_text(file_bytes: bytes) -> str:
    """Extract full text from all PDF pages, separated by page markers."""
    pages = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for i, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            pages.append(f"=== PAGINA {i + 1} ===\n{text}")
    return "\n\n".join(pages)


# ── MAIN PARSE FUNCTION ───────────────────────────────────────────────────────

def parse_investment_pdf(file_bytes: bytes) -> dict:
    """
    Parse a Trade Republic investment PDF for account 0716599502.
    Returns: {"month": "YYYY-MM", "positions": [{"isin", "name", "type", "value"}]}
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise Exception("ANTHROPIC_API_KEY non impostata")

    client = anthropic.Anthropic(api_key=api_key)

    # Step 1: Extract raw text from all PDF pages
    pdf_text = extract_pdf_text(file_bytes)

    # Step 2: Claude extracts structured data (ISIN, raw name, value, date)
    r1 = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2048,
        system=EXTRACT_PROMPT,
        messages=[{"role": "user", "content": f"Analizza questo estratto conto:\n\n{pdf_text}"}],
    )
    raw1 = r1.content[0].text.strip()
    if "```" in raw1:
        raw1 = raw1.split("```")[1]
        if raw1.startswith("json"):
            raw1 = raw1[4:]
    parsed = json.loads(raw1.strip())

    month = parsed.get("month", "")
    positions = parsed.get("positions", [])

    if not positions:
        return {"month": month, "positions": []}

    # Step 3: Claude simplifies names (separate focused call)
    payload = [{"isin": p["isin"], "nome_raw": p["nome_raw"]} for p in positions]
    r2 = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1024,
        system=SIMPLIFY_PROMPT,
        messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
    )
    raw2 = r2.content[0].text.strip()
    if "```" in raw2:
        raw2 = raw2.split("```")[1]
        if raw2.startswith("json"):
            raw2 = raw2[4:]
    names_map = {item["isin"]: item["name"] for item in json.loads(raw2.strip())}

    # Step 4: Build final result — type detected deterministically in Python
    result_positions = []
    for p in positions:
        isin = p["isin"]
        result_positions.append({
            "isin": isin,
            "name": names_map.get(isin, p["nome_raw"]),
            "type": detect_type(p["nome_raw"]),
            "value": float(p.get("valore", 0)),
        })

    return {"month": month, "positions": result_positions}
