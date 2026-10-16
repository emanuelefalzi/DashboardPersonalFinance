"""
"Chiedi alla tua BI" — risponde in linguaggio naturale a domande sui dati
finanziari dell'utente. SOLA LETTURA: non modifica nulla.

Riceve un contesto JSON di aggregati (costruito in app.py) e lo passa a Claude
con la domanda. Istruito a usare SOLO i dati forniti, niente invenzioni.
"""

import os
import json
import anthropic
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"), override=True)

MODEL = "claude-sonnet-4-6"

SYSTEM_PROMPT = """Sei l'assistente di una dashboard di finanza personale italiana.
Rispondi in italiano, conciso e diretto, USANDO SOLO i dati forniti nel contesto JSON.

Regole ferree:
- Usa esclusivamente i numeri presenti nel contesto. NON inventare nulla.
- Se un'informazione non è ricavabile dai dati, dillo ("Non ho questo dato").
- Importi in euro col simbolo €. Mostra i conti quando aiutano (es. "Pasto 120€ + Caffè 30€ = 150€").
- Le spese sono per categoria e per mese (chiave "YYYY-MM"). "guadagno" sugli investimenti
  è valore_attuale − carico. La liquidità è il cash sui conti.
- I rimborsi sono già esclusi dalle spese/entrate.
- INVESTIMENTI — flussi: "flussi_investimenti_per_anno" e "..._per_mese" contengono
  "versato" (soldi messi negli investimenti = acquisti, transazioni in USCITA di tipo investimento),
  "disinvestito" (soldi rientrati dalle vendite) e "netto" (versato − disinvestito, col segno).
  Per "quanto ho investito/versato nel 2026" usa flussi_investimenti_per_anno["2026"]["versato"].
  NON confondere "versato" (flusso, quanto hai messo) con "carico" (costo dei titoli ancora posseduti).
- Sii breve e vai al punto; niente disclaimer inutili."""


def answer_question(question, context):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return "⚠️ Chiave API non configurata: la funzione 'Chiedi alla BI' non è disponibile."

    client = anthropic.Anthropic(api_key=api_key)
    user_msg = (
        f"DATI FINANZIARI (JSON):\n{json.dumps(context, ensure_ascii=False)}\n\n"
        f"DOMANDA: {question}"
    )
    resp = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    return resp.content[0].text.strip()
