#app.py
"""Web app locale: form di input -> calcolo in un thread -> avanzamento -> risultati.

Avvio:  .venv/bin/python app.py   poi aprire http://127.0.0.1:5000
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request

from cutting.model import MAX_INFISSI, MAX_PEZZI, InputError, parse_input
from cutting.report import render_fragment, render_standalone, render_text
from cutting.solver import LEVELS, solve
from cutting.validator import validate

app = Flask(__name__)

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_run_lock = threading.Lock()   # un calcolo alla volta: il solver usa un solo core


def _run(job_id: str, inst, level: str, data: dict) -> None:
    job = _jobs[job_id]

    def progress(frac: float) -> None:
        job["progresso"] = round(frac, 4)

    with _run_lock:
        job["stato"] = "in_corso"
        try:
            sol = solve(inst, level=level, progress=progress)
            # validatore indipendente: se fallisce la soluzione non viene mostrata
            check = validate(sol, data)
            if not check["valida"]:
                job["stato"] = "errore"
                job["titolo"] = "Soluzione scartata dal validatore"
                job["errori"] = check["errori"]
                return
            sol["validazione"] = check
            job["soluzione"] = sol
            job["risultato"] = {"report_html": render_fragment(sol), "hash": sol["hash"]}
            job["stato"] = "completato"
        except Exception as exc:  # errore inatteso del solver: lo mostriamo all'utente
            job["stato"] = "errore"
            job["errori"] = [f"Errore interno del solver: {exc}"]


@app.get("/")
def index():
    return render_template("index.html", livelli=list(LEVELS), max_infissi=MAX_INFISSI, max_pezzi=MAX_PEZZI)


@app.post("/api/calcola")
def calcola():
    data = request.get_json(silent=True)
    level = (data or {}).get("livello", "standard")
    if level not in LEVELS:
        return jsonify({"errori": [f"Livello sconosciuto: {level!r}."]}), 400
    try:
        inst = parse_input(data)
    except InputError as e:
        return jsonify({"errori": e.errors}), 400
    job_id = uuid.uuid4().hex
    with _jobs_lock:
        _jobs[job_id] = {"stato": "in_coda", "progresso": 0.0}
    threading.Thread(target=_run, args=(job_id, inst, level, data), daemon=True).start()
    return jsonify({"job": job_id})


@app.get("/api/job/<job_id>")
def job(job_id: str):
    j = _jobs.get(job_id)
    if j is None:
        return jsonify({"errori": ["Calcolo non trovato."]}), 404
    return jsonify({k: v for k, v in j.items() if k != "soluzione"})


_CSS = Path(__file__).with_name("static") / "report.css"


@app.get("/api/job/<job_id>/<fmt>")
def download(job_id: str, fmt: str):
    """Scarica la soluzione validata: json (completa), html (piano stampabile), txt (testo)."""
    j = _jobs.get(job_id)
    if j is None or "soluzione" not in j:
        return jsonify({"errori": ["Soluzione non disponibile."]}), 404
    sol = j["soluzione"]
    name = f"piano_taglio_{sol['hash'][:8]}"
    if fmt == "json":
        body, mime = json.dumps(sol, indent=2, ensure_ascii=False), "application/json"
    elif fmt == "html":
        body, mime = render_standalone(sol, _CSS.read_text(encoding="utf-8")), "text/html"
    elif fmt == "txt":
        body, mime = render_text(sol), "text/plain"
    else:
        return jsonify({"errori": [f"Formato sconosciuto: {fmt}."]}), 404
    disp = "inline" if request.args.get("apri") else "attachment"
    return Response(body, content_type=mime + "; charset=utf-8",
                    headers={"Content-Disposition": f'{disp}; filename="{name}.{fmt}"'})


if __name__ == "__main__":
    # porta modificabile con la variabile d'ambiente PORTA (predefinita 5000)
    app.run(host="127.0.0.1", port=int(os.environ.get("PORTA", "5000")), debug=False, threaded=True)
