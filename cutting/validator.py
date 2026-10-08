#validator.py
"""Validatore indipendente (Fase 4, par. 12).

Non usa nessuna struttura del solver: rilegge l'input con un parser proprio (stesse regole
della specifica, sez. 3-4), ricostruisce i gruppi e ricalcola tutto dalla SOLA sequenza di
eventi (OPEN, CUT, STANDBY, RESUME, COMPLETE, CAMBIO_MISURA). Dall'esterno importa solo i
valori predefiniti dei parametri, che fanno parte della definizione dell'input.

    validate(soluzione, input) -> {"valida": bool, "errori": [...], "ricalcolato": {...}}

Se "valida" e' False la soluzione non va mostrata.
"""
from __future__ import annotations

import hashlib
import json

from .model import DEFAULT_PARAMS

MAX_ERRORS = 50


def _mm(value) -> int:
    """cm (max 1 decimale) -> mm interi. Solleva ValueError se non rappresentabile."""
    if isinstance(value, bool):
        raise ValueError(value)
    v = float(str(value).replace(",", "."))
    mm = round(v * 10)
    if abs(v * 10 - mm) > 1e-6:
        raise ValueError(value)
    return mm


def _cm(mm: int) -> float:
    return round(mm / 10, 1)


def _parse(data: dict) -> tuple[dict, dict, list[str]]:
    """Rilegge l'input. Restituisce (parametri in mm, gruppi attesi, errori di input)."""
    errs: list[str] = []
    p = dict(DEFAULT_PARAMS)
    p.update((data or {}).get("parametri") or {})
    try:
        L, k = _mm(p["lunghezza_pannello"]), _mm(p["kerf"])
        lam = _mm(p["scarto_extra_max_per_cambio"])
        cap = L - _mm(p.get("margini", 0)) - _mm(p.get("zone_non_tagliabili", 0))
        S = int(p["posti_appoggio"])
    except (TypeError, ValueError, KeyError) as exc:
        return {}, {}, [f"Parametri non validi: {exc}"]
    if cap <= 0 or S < 0 or k < 0:
        errs.append("Parametri non validi (capacità utile, kerf o posti di appoggio).")
    par = {"L": L, "k": k, "lam": lam, "cap": cap, "S": S}

    groups: dict[str, dict] = {}
    for n, inf in enumerate((data or {}).get("infissi") or [], start=1):
        iid = str(inf.get("id_infisso", "")).strip() or str(n)
        lati = inf.get("lati") or []
        try:
            lati = [(_mm(l["misura"]), int(l["quantita"])) for l in lati]
        except (TypeError, ValueError, KeyError):
            errs.append(f"Infisso {iid}: lati non leggibili.")
            continue
        misure = sorted({m for m, _ in lati}, reverse=True)
        if len(misure) > 2:
            errs.append(f"Infisso {iid}: più di due misure distinte — input da rifiutare.")
            continue
        if len(lati) != 2:
            errs.append(f"Infisso {iid}: servono esattamente due lati.")
            continue
        if any(q <= 0 for _, q in lati):
            errs.append(f"Infisso {iid}: quantità non positiva — input da rifiutare.")
            continue
        if any(m + k > cap for m, _ in lati):
            errs.append(f"Infisso {iid}: misura più lunga della capacità utile — input da rifiutare.")
            continue
        (m1, q1), (m2, q2) = lati
        if m1 == m2:
            groups[f"G{n}"] = {"infisso": iid, "misura": m1, "qta": q1 + q2}
        else:
            (mh, qh), (ml, ql) = sorted(lati, key=lambda x: -x[0])
            groups[f"G{n}_H"] = {"infisso": iid, "misura": mh, "qta": qh}
            groups[f"G{n}_L"] = {"infisso": iid, "misura": ml, "qta": ql}
    return par, groups, errs


def validate(sol: dict, data: dict) -> dict:
    err: list[str] = []

    def bad(msg: str) -> None:
        if len(err) < MAX_ERRORS:
            err.append(msg)

    par, groups, input_errs = _parse(data)
    if input_errs:   # (8) input da rifiutare: nessuna soluzione puo' essere valida
        return {"valida": False, "errori": input_errs, "ricalcolato": {}}
    L, k, lam, cap, S = par["L"], par["k"], par["lam"], par["cap"], par["S"]

    # parametri dichiarati nella soluzione coerenti con l'input
    sp = sol.get("parametri", {})
    for key, val in (("lunghezza_pannello", L), ("kerf", k), ("capacita_utile", cap)):
        try:
            if _mm(sp.get(key)) != val:
                bad(f"Parametro '{key}' della soluzione ({sp.get(key)}) diverso dall'input ({_cm(val)}).")
        except (TypeError, ValueError):
            bad(f"Parametro '{key}' mancante o non valido nella soluzione.")
    if sp.get("posti_appoggio") != S:
        bad(f"posti_appoggio della soluzione ({sp.get('posti_appoggio')}) diverso dall'input ({S}).")

    # ---- simulazione della sequenza di eventi ------------------------------------------
    state: dict[str, str] = {}       # pannello -> NON_APERTO assente / IN_MACCHINA / STANDBY / COMPLETATO
    used: dict[str, int] = {}        # pannello -> mm consumati (pezzi + kerf)
    cuts_of: dict[str, list[int]] = {}   # pannello -> posizioni (indice di taglio) dei suoi tagli
    open_order: list[str] = []
    in_machine: str | None = None
    setting: int | None = None       # misura impostata in macchina
    n_change_events = 0
    cut_seq: list[tuple[str, int]] = []   # (group_id, misura) in ordine di taglio
    pieces_seen: set[str] = set()
    count: dict[str, int] = {}
    standby_max = 0
    z2 = 0
    n_standby_ev = 0
    expected_n = 1
    numbering_reported = False

    for e in sol.get("eventi", []):
        n, t, pid = e.get("n"), e.get("tipo"), e.get("pannello")
        where = f"evento {n} ({t}{' ' + pid if pid else ''})"
        if n != expected_n and not numbering_reported:   # segnalato una volta sola
            bad(f"{where}: numerazione eventi non consecutiva (atteso {expected_n}).")
            numbering_reported = True
        expected_n += 1

        if t == "CAMBIO_MISURA":
            try:
                m = _mm(e.get("misura"))
            except (TypeError, ValueError):
                bad(f"{where}: misura non valida."); continue
            first = setting is None
            if bool(e.get("setup_iniziale")) != first:
                bad(f"{where}: flag setup_iniziale errato.")
            if not first:
                if m == setting:
                    bad(f"{where}: cambio misura verso la misura già impostata.")
                n_change_events += 1
            setting = m
        elif t == "OPEN":
            if pid in state:
                bad(f"{where}: pannello già aperto in precedenza.")
            if in_machine is not None:
                bad(f"{where}: in macchina c'è ancora {in_machine}.")
            if pid != f"P{len(open_order) + 1}":
                bad(f"{where}: gli id pannello devono seguire l'ordine di apertura (atteso P{len(open_order) + 1}).")
            state[pid] = "IN_MACCHINA"; in_machine = pid
            used[pid] = 0; cuts_of[pid] = []; open_order.append(pid)
        elif t == "CUT":
            if pid != in_machine:
                bad(f"{where}: il pannello non è in macchina (in macchina: {in_machine}).")
            gid = e.get("group_id")
            g = groups.get(gid)
            try:
                m = _mm(e.get("misura"))
            except (TypeError, ValueError):
                bad(f"{where}: misura non valida."); continue
            if g is None:
                bad(f"{where}: gruppo '{gid}' inesistente nell'input."); continue
            if m != g["misura"]:
                bad(f"{where}: misura {_cm(m)} diversa da quella del gruppo {gid} ({_cm(g['misura'])}).")
            if e.get("id_infisso") != g["infisso"]:
                bad(f"{where}: infisso '{e.get('id_infisso')}' diverso da quello del gruppo {gid} ('{g['infisso']}').")
            if setting != m:
                bad(f"{where}: la macchina è impostata su {None if setting is None else _cm(setting)}, non su {_cm(m)}.")
            pz = e.get("id_pezzo")
            if not isinstance(pz, str) or not pz.startswith(gid + ".") or pz in pieces_seen:
                bad(f"{where}: id_pezzo '{pz}' non valido o duplicato.")
            pieces_seen.add(pz)
            count[gid] = count.get(gid, 0) + 1
            if pid in used:
                used[pid] += g["misura"] + k
                cuts_of[pid].append(len(cut_seq))
            cut_seq.append((gid, g["misura"]))
        elif t == "STANDBY":
            if pid != in_machine:
                bad(f"{where}: solo il pannello in macchina può andare in standby.")
            elif not cuts_of.get(pid):
                bad(f"{where}: un pannello senza tagli non può andare in standby.")
            state[pid] = "STANDBY"; in_machine = None
            n_standby_ev += 1
        elif t == "RESUME":
            if state.get(pid) != "STANDBY":
                bad(f"{where}: si può riprendere solo un pannello in standby.")
            if in_machine is not None:
                bad(f"{where}: in macchina c'è ancora {in_machine}.")
            state[pid] = "IN_MACCHINA"; in_machine = pid
        elif t == "COMPLETE":
            if pid != in_machine:
                bad(f"{where}: si può completare solo il pannello in macchina.")
            state[pid] = "COMPLETATO"
            if in_machine == pid:
                in_machine = None
        else:
            bad(f"{where}: tipo di evento sconosciuto.")

        # (3) standby dopo ogni evento, pannello in macchina escluso
        sb = [p for p in open_order if state.get(p) == "STANDBY"]
        standby_max = max(standby_max, len(sb))
        if len(sb) > S:
            bad(f"{where}: pannelli in standby {len(sb)}, oltre i {S} posti di appoggio.")
        z2 += sum(cap - used[p] for p in sb)

    # (4) stato finale
    if in_machine is not None:
        bad(f"Fine sequenza: {in_machine} è ancora in macchina.")
    for p in open_order:
        if state[p] != "COMPLETATO":
            bad(f"Fine sequenza: {p} non è completato (stato {state[p]}).")

    # (1) quantita' per gruppo, infisso e misura
    for gid in sorted(groups):
        if count.get(gid, 0) != groups[gid]["qta"]:
            bad(f"Gruppo {gid}: tagliati {count.get(gid, 0)} pezzi, richiesti {groups[gid]['qta']}.")
    by_inf_req: dict[tuple, int] = {}
    by_inf_cut: dict[tuple, int] = {}
    for gid, g in groups.items():
        key = (g["infisso"], g["misura"])
        by_inf_req[key] = by_inf_req.get(key, 0) + g["qta"]
        by_inf_cut[key] = by_inf_cut.get(key, 0) + count.get(gid, 0)
    for key in sorted(by_inf_req):
        if by_inf_req[key] != by_inf_cut[key]:
            bad(f"Infisso {key[0]}, misura {_cm(key[1])}: tagliati {by_inf_cut[key]}, richiesti {by_inf_req[key]}.")

    # (2) capacita' con kerf su ogni taglio
    for p in open_order:
        if used[p] > cap:
            bad(f"{p}: pezzi + kerf = {_cm(used[p])} cm oltre la capacità utile {_cm(cap)} cm.")
        if not cuts_of[p]:
            bad(f"{p}: pannello aperto senza tagli.")

    # (5) contiguita' di ogni group_id (uguaglianza esatta di stringa)
    first_pos: dict[str, int] = {}
    last_pos: dict[str, int] = {}
    for i, (gid, _) in enumerate(cut_seq):
        first_pos.setdefault(gid, i)
        last_pos[gid] = i
    for gid in sorted(first_pos):
        span = last_pos[gid] - first_pos[gid] + 1
        if span != count[gid]:
            bad(f"Gruppo {gid} spezzato: i suoi {count[gid]} pezzi occupano {span} posizioni nella sequenza dei tagli.")

    # (6) cambi misura ricalcolati dalla sequenza dei tagli (primo setup escluso)
    changes = sum(1 for i in range(1, len(cut_seq)) if cut_seq[i][1] != cut_seq[i - 1][1])
    if changes != n_change_events:
        bad(f"Cambi misura: {changes} dai tagli consecutivi, {n_change_events} eventi CAMBIO_MISURA.")

    # (7) scarto per pannello, totale e costo
    scarti = {p: cap - used[p] for p in open_order}
    tot = sum(scarti.values())
    cost = tot + lam * changes
    ml = min((g["misura"] for g in groups.values()), default=0)
    z3a = sum(cuts_of[p][-1] - cuts_of[p][0] for p in open_order if cuts_of[p])
    z3c = sum(1 for p in open_order if scarti[p] >= ml + k)
    asc = 0
    prev = None
    for i, (gid, m) in enumerate(cut_seq):
        if i == 0 or gid != cut_seq[i - 1][0]:
            if prev is not None and m > prev:
                asc += 1
            prev = m

    ric = {
        "pannelli": len(open_order),
        "cambi_misura": changes,
        "scarto_totale_cm": _cm(tot),
        "costo_cm": _cm(cost),
        "scarto_per_pannello_cm": {p: _cm(v) for p, v in scarti.items()},
        "standby_massimo": standby_max,
        "posti_appoggio": S,
        "obiettivo_2": _cm(z2),
        "obiettivo_3a": z3a,
        "obiettivo_3b": n_standby_ev,
        "obiettivo_3c": z3c,
        "inversioni_ordine_misure": asc,
        "avanzo_massimo_cm": _cm(max(scarti.values(), default=0)),
        "avanzo_massimo_pannello": max(open_order, key=lambda q: (scarti[q], -open_order.index(q)), default=None),
    }

    # i valori dichiarati dal solver devono coincidere con quelli ricalcolati
    dich = sol.get("dichiarato_dal_solver", {})
    for key in ("pannelli", "cambi_misura", "scarto_totale_cm", "costo_cm", "obiettivo_2",
                "obiettivo_3a", "obiettivo_3b", "obiettivo_3c", "inversioni_ordine_misure",
                "avanzo_massimo_cm"):
        if key in dich and dich[key] != ric[key]:
            bad(f"Valore dichiarato '{key}' = {dich[key]} diverso da quello ricalcolato = {ric[key]}.")

    # hash: ricalcolato sulla stessa serializzazione canonica (parametri + eventi)
    payload = json.dumps({"parametri": sp, "eventi": sol.get("eventi", [])},
                         sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    if hashlib.sha256(payload.encode()).hexdigest() != sol.get("hash"):
        bad("L'hash della soluzione non corrisponde ai suoi eventi.")

    return {"valida": not err, "errori": err, "ricalcolato": ric}
