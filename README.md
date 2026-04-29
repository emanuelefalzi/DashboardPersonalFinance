# Finance Dashboard

Dashboard personale per tracciare spese, investimenti e patrimonio. Importa estratti conto da Banca Generali e Trade Republic, categorizza le transazioni automaticamente con AI (Claude di Anthropic) e visualizza tutto in un'interfaccia web.

---

## Requisiti

- **Python 3.9+** — [scarica da python.org](https://www.python.org/downloads/)
- **Una chiave API Anthropic** (per la categorizzazione automatica con AI) — [crea un account su console.anthropic.com](https://console.anthropic.com/)

---

## Installazione

### 1. Installa le dipendenze Python

Apri il Terminale, vai nella cartella del progetto ed esegui:

```bash
cd percorso/della/cartella
pip3 install -r requirements.txt
```

### 2. Configura la chiave API

Rinomina il file `.env.example` in `.env` e sostituisci il testo segnaposto con la tua chiave API Anthropic:

```
ANTHROPIC_API_KEY=sk-ant-api03-LA_TUA_CHIAVE_QUI
```

La chiave si trova su [console.anthropic.com](https://console.anthropic.com/) → API Keys.

> Se non hai una chiave API, la dashboard funziona comunque — le transazioni importate non verranno categorizzate automaticamente ma potrai farlo a mano dallo Storico.

---

## Avvio

### Modo facile (macOS)
Doppio clic sul file **`Avvia Dashboard.command`** nella cartella del progetto. Si apre il Terminale e il server parte da solo.

### Da Terminale
```bash
cd percorso/della/cartella/app
python3 app.py
```

Poi apri il browser su: **[http://localhost:5001](http://localhost:5001)**

Per spegnere il server: premi `Ctrl+C` nel Terminale.

---

## Come si usa

### Prima configurazione (Onboarding)
Vai su **Onboarding** nella barra laterale e inserisci i saldi di partenza per ciascun conto (Trade Republic, Banca Generali, PayPal) e l'importo investito inizialmente. Questi valori servono come punto di riferimento per i calcoli di patrimonio e rendimento.

### Importare le transazioni
1. Vai su **Storico**
2. Clicca su **Importa** e carica il file CSV o XLSX esportato dalla tua banca
   - **Banca Generali**: esporta da web banking come Excel/CSV
   - **Trade Republic**: esporta da app come CSV
3. La AI categorizza automaticamente ogni transazione
4. Puoi correggere categoria, tipo e descrizione direttamente nella tabella

### Importare i dati investimenti
1. Vai su **Overview Patrimonio**
2. Carica il PDF del rendiconto mensile di Trade Republic
3. I valori di ETF e azioni vengono estratti automaticamente

### Dashboard
Mostra il riepilogo del mese selezionato:
- **Spese per categoria** (grafico a torta)
- **Patrimonio** (depositi + valore investimenti)
- **Valore investimenti** con controvalore di carico e guadagno/perdita
- **Grafico andamento patrimonio** (anno corrente, navigabile)

---

## Struttura file

```
├── app/
│   ├── app.py               # Server Flask + API
│   ├── file_parser.py       # Parser CSV/XLSX delle banche
│   ├── ai_categorizer.py    # Categorizzazione AI con Claude
│   └── investment_parser.py # Estrazione dati da PDF Trade Republic
├── data/
│   ├── transactions.csv     # Tutte le transazioni (generato automaticamente)
│   ├── investments.json     # Valori ETF/azioni per mese
│   ├── onboarding.json      # Saldi iniziali
│   └── categories.json      # Categorie personalizzate
├── static/
│   └── style.css
├── templates/
│   └── index.html
├── requirements.txt
└── .env                     # Chiave API (NON condividere questo file)
```

---

## Note importanti

- Il file **`.env`** contiene la tua chiave API — **non condividerlo mai** e non caricarlo su GitHub o servizi cloud
- I file nella cartella **`data/`** contengono i tuoi dati finanziari personali — trattali con la stessa attenzione
- Tutti i dati vengono salvati localmente sul tuo computer, nessun dato viene inviato a server esterni (tranne le descrizioni delle transazioni che vengono mandate ad Anthropic per la categorizzazione AI)
