#!/usr/bin/env bash
# Avvia la web app di ottimizzazione tagli e apre il browser su http://127.0.0.1:5000
# Uso:  ./avvia.sh        (fermare con Ctrl+C)
cd "$(dirname "$0")" || exit 1

if [ ! -x .venv/bin/python ]; then
  echo "Ambiente virtuale mancante: lo creo e installo le dipendenze..."
  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt || exit 1
fi

if ss -ltn 2>/dev/null | grep -q ':5000 '; then
  echo "La porta 5000 è già in uso: il servizio è forse già attivo. Apro il browser."
  xdg-open http://127.0.0.1:5000 >/dev/null 2>&1 &
  exit 0
fi

( sleep 2; xdg-open http://127.0.0.1:5000 >/dev/null 2>&1 ) &
exec .venv/bin/python app.py
