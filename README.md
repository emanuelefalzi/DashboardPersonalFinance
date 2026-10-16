# Finance Dashboard

A self-hosted personal finance dashboard that runs on your own computer. It imports bank and broker statements, uses Claude to categorise every transaction, and shows spending, net worth and investments in one web interface.

## Why it exists

My money sits in a current account, a broker and PayPal, and each one exports statements in its own format. Banking apps tell you what you spent, not where your net worth is going or how much risk your portfolio carries. I built this to see everything in one place, with categorisation that understands Italian bank descriptions and no manual tagging.

## What it does

- **Multi-bank import**: CSV/XLSX statements from Banca Generali and Trade Republic, plus Trade Republic's monthly PDF reports for ETF and stock values ([`file_parser.py`](app/file_parser.py), [`investment_parser.py`](app/investment_parser.py)).
- **AI categorisation**: Claude reads each transaction's raw bank description and assigns category and type; more than 700 transactions categorised so far. You can correct any row in the table ([`ai_categorizer.py`](app/ai_categorizer.py)).
- **Cost basis**: weighted-average cost per ISIN, realised profit on each sale, and warnings for sales without a matching purchase ([`cost_basis.py`](app/cost_basis.py)).
- **Live prices**: ISIN to EUR price from Tradegate, with Yahoo Finance as fallback and a file cache so the dashboard still works offline ([`price_feed.py`](app/price_feed.py)).
- **Portfolio risk**: unit prices rebuilt from lots (so new purchases do not count as returns), annualised volatility, an SRRI risk class, and concentration through HHI and effective number of holdings ([`metrics.py`](app/metrics.py)).
- **"Ask your BI"**: questions in plain language about your own figures, answered by Claude from aggregated data only, read-only ([`ai_bi.py`](app/ai_bi.py)).
- **Reimbursement matching**: Claude proposes which incoming transfers reimburse which expenses; nothing is applied until you confirm ([`ai_reimburse.py`](app/ai_reimburse.py)).

## Tech stack

Python, Flask, pandas, pdfplumber, openpyxl, the Anthropic Python SDK. Plain HTML/CSS/JS front end. Data stays in local CSV/JSON files.

## Running it

Requirements: Python 3.9+ and an [Anthropic API key](https://console.anthropic.com/) (optional: without it, everything works except automatic categorisation).

```bash
pip3 install -r requirements.txt
cp env.example .env          # then put your ANTHROPIC_API_KEY in .env
cp data/personal.example.json data/personal.json   # optional: your names for the AI prompts
cd app && python3 app.py
```

Open [http://localhost:5001](http://localhost:5001). On macOS you can also double-click `Avvia Dashboard.command`.

First run: open **Onboarding** and enter the starting balance of each account, then go to **Storico → Importa** and upload a statement.

> The UI is in Italian, because I built it for myself.

## Privacy

Everything is stored locally in `data/`, which is gitignored apart from the example files. Only transaction descriptions and aggregated figures are sent to Anthropic for categorisation and questions. `.env` and `data/personal.json` must never be committed.

## How it was built

I designed the features, the categorisation rules and the data model, and tested them on my own statements. The code was written with Claude Code.
