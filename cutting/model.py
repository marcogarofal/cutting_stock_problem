#model.py
"""Parsing e validazione dell'input, conversione in interi (mm), assegnazione group_id.

Tutte le grandezze interne sono interi in millimetri (1 cm = 10 unita').
"""
from __future__ import annotations

from dataclasses import dataclass, field

MAX_INFISSI = 40
MAX_PEZZI = 320

DEFAULT_PARAMS = {
    "lunghezza_pannello": 650.0,
    "kerf": 0.3,
    "posti_appoggio": 2,
    "scarto_extra_max_per_cambio": 20.0,
    "margini": 15.0,
    "zone_non_tagliabili": 0.0,
    "ordine_misure": "preferito",
}

# "decrescente": regola rigida, i blocchi macchina vanno dalla misura piu' lunga alla piu' corta
#                (a parita' di misura, ordine degli infissi).
# "preferito":   prima l'Obiettivo 1 (scarto + cambi), poi, a parita', meno inversioni
#                dell'ordine decrescente (misura piu' lunga dopo una piu' corta).
# "libero":      l'ordine lo sceglie il solver solo in base agli obiettivi della specifica.
ORDINI_MISURE = ("decrescente", "preferito", "libero")


class InputError(ValueError):
    """Input rifiutato. `errors` contiene i messaggi leggibili."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass(frozen=True)
class Group:
    idx: int            # posizione nell'elenco dei gruppi (ordine canonico)
    gid: str            # es. "G3_H", "G3_L", "G3"
    inf_id: str         # id_infisso dell'utente
    inf_ord: int        # numero base (1, 2, ...) = ordine di input
    length: int         # mm
    qty: int


@dataclass(frozen=True)
class Instance:
    capacity: int               # capacita' utile, mm
    panel_length: int           # lunghezza nominale, mm
    kerf: int                   # mm
    standby_slots: int
    change_weight: int          # mm di scarto equivalenti a un cambio misura
    groups: tuple[Group, ...]
    infissi: tuple[str, ...]    # id_infisso in ordine di input
    ordine_misure: str = "preferito"
    raw_params: dict = field(default_factory=dict, compare=False)

    @property
    def n_pieces(self) -> int:
        return sum(g.qty for g in self.groups)

    @property
    def min_length(self) -> int:
        return min(g.length for g in self.groups)


def cm_to_mm(value, name: str, errors: list[str], allow_zero: bool = True) -> int | None:
    """Converte un valore in cm (max 1 decimale) in mm interi."""
    try:
        if isinstance(value, bool):
            raise TypeError
        v = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        errors.append(f"{name}: valore non numerico ({value!r}).")
        return None
    mm = round(v * 10)
    if abs(v * 10 - mm) > 1e-6:
        errors.append(f"{name}: massimo 1 decimale ammesso (ricevuto {value}).")
        return None
    if mm < 0 or (mm == 0 and not allow_zero):
        errors.append(f"{name}: deve essere {'>= 0' if allow_zero else '> 0'} (ricevuto {value}).")
        return None
    return mm


def mm_to_cm(mm: int) -> float:
    return round(mm / 10, 1)


def _to_int(value, name: str, errors: list[str]) -> int | None:
    try:
        if isinstance(value, bool):
            raise TypeError
        f = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        errors.append(f"{name}: valore intero richiesto ({value!r}).")
        return None
    if f != int(f):
        errors.append(f"{name}: valore intero richiesto ({value!r}).")
        return None
    return int(f)


def parse_input(data: dict) -> Instance:
    """Valida l'input JSON e costruisce l'istanza. Solleva InputError."""
    errors: list[str] = []
    if not isinstance(data, dict):
        raise InputError(["L'input deve essere un oggetto JSON con 'parametri' e 'infissi'."])

    params = dict(DEFAULT_PARAMS)
    params.update(data.get("parametri") or {})

    panel = cm_to_mm(params["lunghezza_pannello"], "lunghezza_pannello", errors, allow_zero=False)
    kerf = cm_to_mm(params["kerf"], "kerf", errors)
    weight = cm_to_mm(params["scarto_extra_max_per_cambio"], "scarto_extra_max_per_cambio", errors)
    margins = cm_to_mm(params.get("margini", 0), "margini", errors)
    zones = cm_to_mm(params.get("zone_non_tagliabili", 0), "zone_non_tagliabili", errors)
    slots = _to_int(params["posti_appoggio"], "posti_appoggio", errors)
    if slots is not None and slots < 0:
        errors.append("posti_appoggio: deve essere >= 0.")
    ordine = params.get("ordine_misure") or "preferito"
    if ordine not in ORDINI_MISURE:
        errors.append(f"ordine_misure: valori ammessi {', '.join(ORDINI_MISURE)} (ricevuto {ordine!r}).")

    capacity = None
    if None not in (panel, margins, zones):
        capacity = panel - margins - zones
        if capacity <= 0:
            errors.append("Capacita' utile del pannello nulla o negativa (lunghezza - margini - zone).")
            capacity = None

    infissi = data.get("infissi")
    if not isinstance(infissi, list) or not infissi:
        errors.append("Serve almeno un infisso.")
        infissi = []
    if len(infissi) > MAX_INFISSI:
        errors.append(f"Massimo {MAX_INFISSI} infissi (ricevuti {len(infissi)}).")

    groups: list[Group] = []
    ids: list[str] = []
    seen: set[str] = set()
    for n, inf in enumerate(infissi, start=1):
        label = f"Infisso #{n}"
        if not isinstance(inf, dict):
            errors.append(f"{label}: formato non valido.")
            continue
        inf_id = str(inf.get("id_infisso", "")).strip() or str(n)
        label = f"Infisso '{inf_id}' (#{n})"
        if inf_id in seen:
            errors.append(f"{label}: id_infisso duplicato.")
        seen.add(inf_id)
        ids.append(inf_id)

        lati = inf.get("lati")
        if not isinstance(lati, list) or not lati:
            errors.append(f"{label}: servono esattamente due lati.")
            continue
        parsed = []
        for li, lato in enumerate(lati, start=1):
            if not isinstance(lato, dict):
                errors.append(f"{label}, lato {li}: formato non valido.")
                continue
            m = cm_to_mm(lato.get("misura"), f"{label}, lato {li}, misura", errors, allow_zero=False)
            q = _to_int(lato.get("quantita"), f"{label}, lato {li}, quantita", errors)
            if q is not None and q <= 0:
                errors.append(f"{label}, lato {li}: quantita' deve essere positiva (ricevuto {q}).")
                q = None
            if m is not None and capacity is not None and m + (kerf or 0) > capacity:
                errors.append(
                    f"{label}, lato {li}: misura {mm_to_cm(m)} cm (+ kerf) piu' lunga della "
                    f"capacita' utile del pannello ({mm_to_cm(capacity)} cm)."
                )
                m = None
            parsed.append((m, q))

        distinct = sorted({m for m, _ in parsed if m is not None}, reverse=True)
        if len(distinct) > 2:
            errors.append(
                f"{label}: più di due misure distinte ({', '.join(str(mm_to_cm(d)) for d in distinct)}); "
                "un infisso ha solo altezza e larghezza."
            )
            continue
        if len(lati) != 2:
            errors.append(f"{label}: servono esattamente due lati (ricevuti {len(lati)}).")
            continue
        if any(m is None or q is None for m, q in parsed):
            continue

        (m1, q1), (m2, q2) = parsed
        if m1 == m2:
            groups.append(Group(0, f"G{n}", inf_id, n, m1, q1 + q2))
        else:
            (mh, qh), (ml, ql) = sorted(parsed, key=lambda p: -p[0])
            groups.append(Group(0, f"G{n}_H", inf_id, n, mh, qh))
            groups.append(Group(0, f"G{n}_L", inf_id, n, ml, ql))

    total = sum(g.qty for g in groups)
    if total > MAX_PEZZI:
        errors.append(f"Massimo {MAX_PEZZI} pezzi (richiesti {total}).")

    if errors:
        raise InputError(errors)

    groups = [Group(i, g.gid, g.inf_id, g.inf_ord, g.length, g.qty) for i, g in enumerate(groups)]
    raw = {
        "lunghezza_pannello": mm_to_cm(panel),
        "kerf": mm_to_cm(kerf),
        "posti_appoggio": slots,
        "scarto_extra_max_per_cambio": mm_to_cm(weight),
        "margini": mm_to_cm(margins),
        "zone_non_tagliabili": mm_to_cm(zones),
        "capacita_utile": mm_to_cm(capacity),
        "ordine_misure": ordine,
    }
    return Instance(
        capacity=capacity,
        panel_length=panel,
        kerf=kerf,
        standby_slots=slots,
        change_weight=weight,
        groups=tuple(groups),
        infissi=tuple(ids),
        ordine_misure=ordine,
        raw_params=raw,
    )
