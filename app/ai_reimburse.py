"""
Rimborsi — abbina i rimborsi (di solito dal padre) alle transazioni.

Lato ENTRATE è facile: i bonifici in entrata, tranne la paghetta, sono ~99% rimborsi.
Lato SPESE serve l'indicazione dell'utente (a parole) su cosa è stato rimborsato.
L'AI propone gli abbinamenti; l'utente CONFERMA sempre prima di applicare.
"""

import os
import json
import anthropic
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"), override=True)

MODEL = "claude-sonnet-4-6"

SYSTEM_PROMPT = """Sei un assistente che abbina i rimborsi alle transazioni di un utente italiano.

Contesto fisso:
- I bonifici IN ENTRATA (tranne la "Paghetta") sono quasi sempre rimborsi dal padre.
- Categorie TIPICAMENTE rimborsate: vestiti/scarpe, benzina/carburante, pesce.
- "spesa a Fiorenzuola" è INCERTA: includila SOLO se l'utente la nomina esplicitamente.
- Il totale delle spese rimborsate deve essere ≈ all'importo dei rimborsi in entrata.

Compito:
1. Tra le ENTRATE fornite, indica quali sono rimborsi (di norma tutte) → "incoming_idx".
2. Tra le SPESE fornite, scegli quelle che corrispondono alla descrizione dell'utente,
   con totale ≈ all'importo dei rimborsi → "expense_idx". Sii prudente sugli ambigui.

Usa SOLO gli _idx forniti. Rispondi SOLO con JSON valido, senza markdown:
{"incoming_idx": [..], "expense_idx": [..], "nota": "breve spiegazione in italiano"}"""


def suggest(text, incoming, expenses):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise Exception("Chiave API non configurata")
    client = anthropic.Anthropic(api_key=api_key)
    payload = {
        "richiesta_utente": text,
        "entrate_possibili_rimborsi": incoming,
        "spese_candidate": expenses,
    }
    resp = client.messages.create(
        model=MODEL, max_tokens=1500, system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
    )
    raw = resp.content[0].text.strip()
    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())
