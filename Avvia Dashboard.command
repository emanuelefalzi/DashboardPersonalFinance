#!/bin/bash
# Avvia la dashboard usando l'ambiente virtuale del progetto (.venv).
# Se manca o mancano le dipendenze, lo crea/aggiorna da requirements.txt.
ROOT="$(cd "$(dirname "$0")" && pwd)"
VENV="$ROOT/.venv"

if [ ! -x "$VENV/bin/python" ]; then
  echo "Creo l'ambiente virtuale..."
  python3 -m venv "$VENV" || { echo "Impossibile creare il venv"; exit 1; }
fi

if ! "$VENV/bin/python" -c "import flask" 2>/dev/null; then
  echo "Installo le dipendenze..."
  "$VENV/bin/pip" install -r "$ROOT/requirements.txt" || exit 1
fi

cd "$ROOT/app"
exec "$VENV/bin/python" app.py
