# run.py - uso da riga di comando:  .venv/bin/python run.py input.json [rapido|standard|esteso]
import json
import sys

from cutting.model import InputError, parse_input
from cutting.solver import solve
from cutting.validator import validate

ESEMPIO = {
    "parametri": {"lunghezza_pannello": 630, "kerf": 0.3, "posti_appoggio": 3,
                  "scarto_extra_max_per_cambio": 20, "margini": 0},
    "infissi": [
        {"id_infisso": "A", "lati": [{"misura": 200, "quantita": 4}, {"misura": 160, "quantita": 4}]},
        {"id_infisso": "B", "lati": [{"misura": 200, "quantita": 4}, {"misura": 160, "quantita": 4}]},
    ],
}

if __name__ == "__main__":
    data = ESEMPIO
    if len(sys.argv) > 1:
        with open(sys.argv[1], encoding="utf-8") as f:
            data = json.load(f)
    level = sys.argv[2] if len(sys.argv) > 2 else "standard"
    try:
        inst = parse_input(data)
    except InputError as e:
        sys.exit("\n".join(e.errors))
    sol = solve(inst, level=level)
    check = validate(sol, data)
    if not check["valida"]:
        sys.exit("Soluzione scartata dal validatore:\n" + "\n".join(check["errori"]))
    sol["validazione"] = check
    print(json.dumps(sol, indent=2, ensure_ascii=False))
