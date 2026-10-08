#solver.py
"""Solver deterministico: DP sugli stati con dominanza + beam search (Fase 2, par. 2.9).

Ogni livello della ricerca corrisponde a una posizione della sequenza globale dei tagli.
Uno stato contiene: gruppi non iniziati (bitmask), gruppo in corso e pezzi rimasti,
misura impostata, pannello in macchina, pannelli in standby, contatori degli obiettivi.

Se nessun livello viene troncato dal beam, la ricerca e' esatta per l'Obiettivo 1
(sono scartati solo stati dominati). Gli obiettivi 2 e 3 sono ottimizzati a parita'
di Obiettivo 1 e sono euristici quando il beam tronca.

Nessuna casualita': tutte le iterazioni avvengono su liste ordinate per chiave esplicita.
"""
from __future__ import annotations

import hashlib
import json
from typing import Callable

from .model import Instance, mm_to_cm

# Ampiezza del beam per livello di budget (numero di stati conservati per posizione).
# Il budget e' deterministico (ampiezza fissa, nessun limite in secondi). Tempi misurati su
# 160 pezzi, 3 posti, caso peggiore con le 4 ricerche del portafoglio (Python 3.12, 1 core):
# rapido ~5 s, standard ~1-1.5 min, esteso ~20 min (misurato: ampiezza 7000 -> 1696 s,
# tempo lineare nell'ampiezza -> 5000 ~ 1200 s; picco memoria ~0.5 GB).
LEVELS = {"rapido": 50, "standard": 500, "esteso": 5000}

INF = 1 << 60

# Oltre 160 pezzi il tempo per ampiezza cresce circa come N^1.7 (misurato: 160 -> 320 pezzi,
# x3.2 a parita' di ampiezza): l'ampiezza di "esteso" viene ridotta di conseguenza per restare
# intorno ai ~20 minuti. Deterministico: dipende solo dal numero di pezzi.
_REF_PIECES = 160


def _level_width(level: str, n_pieces: int) -> int:
    w = LEVELS[level]
    if level == "esteso" and n_pieces > _REF_PIECES:
        w = max(LEVELS["standard"], round(w * (_REF_PIECES / n_pieces) ** 1.7))
    return w


class _St:
    __slots__ = ("rem", "cur", "left", "m", "mac", "sb", "npan", "nch", "ev",
                 "z2", "z3a", "z3b", "z3c", "asc", "wl", "need", "parent", "evs", "tb", "key", "rank", "prof")


def _leftover_key(wastes) -> tuple:
    """Criterio "avanzo": scarti dei pannelli in ordine decrescente, da massimizzare
    lessicograficamente (prima l'avanzo piu' lungo, poi il secondo, ...). Negato per min()."""
    return tuple(-w for w in sorted(wastes, reverse=True))


def _l2_bound(sizes: list[int], cap: int) -> int:
    """Lower bound L2 di Martello-Toth per il bin packing."""
    best = 0
    alphas = sorted({0} | {s for s in sizes if s <= cap // 2})
    for a in alphas:
        j1 = [s for s in sizes if s > cap - a]
        j2 = [s for s in sizes if cap - a >= s > cap / 2]
        j3 = [s for s in sizes if cap / 2 >= s >= a]
        free = len(j2) * cap - sum(j2)
        extra = max(0, -(-(sum(j3) - free) // cap))
        best = max(best, len(j1) + len(j2) + extra)
    return best


def lower_bounds(inst: Instance) -> dict:
    k, C = inst.kerf, inst.capacity
    sizes = [g.length + k for g in inst.groups for _ in range(g.qty)]
    # cardinalita': un pannello contiene al massimo `kmax` pezzi (i piu' corti)
    kmax, acc = 0, 0
    for s_ in sorted(sizes):
        if acc + s_ > C:
            break
        acc += s_
        kmax += 1
    # riempimento massimo di un pannello: somma ottenibile piu' grande <= C
    reach = 1
    full = (1 << (C + 1)) - 1
    for s_ in sorted(set(sizes)):
        step = s_
        while step <= C:
            reach |= (reach << step) & full
            step <<= 1
    fill = reach.bit_length() - 1
    lb_p = max(-(-sum(sizes) // C), _l2_bound(sizes, C),
               -(-len(sizes) // kmax), -(-sum(sizes) // fill))
    lb_ch = len({g.length for g in inst.groups}) - 1
    return {"pannelli": lb_p, "cambi": lb_ch, "z1": C * lb_p + inst.change_weight * lb_ch}


class Solver:
    def __init__(self, inst: Instance, width: int, slots: int | None = None,
                 progress: Callable[[int], None] | None = None):
        self.inst = inst
        self.width = width
        self.slots = inst.standby_slots if slots is None else slots
        self.pref_desc = inst.ordine_misure == "preferito"
        self.progress = progress
        self.groups = inst.groups
        k = inst.kerf
        self.need = [g.length + k for g in self.groups]
        self.lmin_k = inst.min_length + k
        # gruppi ordinati per lunghezza crescente, per il minimo residuo utile
        self.by_len = sorted(range(len(self.groups)), key=lambda i: (self.groups[i].length, i))
        lengths = sorted({g.length for g in self.groups}, reverse=True)
        self.len_masks = [(L, sum(1 << g.idx for g in self.groups if g.length == L)) for L in lengths]
        self._minlen_cache: dict[int, int] = {}
        self._cands_cache: dict[int, tuple[int, ...]] = {}
        self._reach_cache: dict[tuple[int, ...], int] = {}
        self._reach_mask_cache: dict[int, int] = {}
        self._nlen_cache: dict[int, int] = {}
        self.len_of = {L: lm for L, lm in self.len_masks}
        self.need_total = sum(self.need[g.idx] * g.qty for g in self.groups)
        self.nodes = 0
        self.truncated = False
        self.truncated_levels = 0

    # ---- utilita' sugli insiemi di gruppi -------------------------------------------------
    def _minlen(self, mask: int) -> int:
        v = self._minlen_cache.get(mask)
        if v is None:
            v = INF
            for i in self.by_len:
                if mask >> i & 1:
                    v = self.groups[i].length
                    break
            self._minlen_cache[mask] = v
        return v

    def _cands(self, mask: int) -> tuple[int, ...]:
        """Gruppi candidati come prossimo: un rappresentante (indice minimo) per (misura, quantita')."""
        v = self._cands_cache.get(mask)
        if v is None:
            seen = set()
            out = []
            # ordine decrescente: solo i gruppi della misura piu' lunga ancora da tagliare
            Lmax = max((g.length for i, g in enumerate(self.groups) if mask >> i & 1), default=0) \
                if self.inst.ordine_misure == "decrescente" else None
            for i, g in enumerate(self.groups):
                if Lmax is not None and g.length != Lmax:
                    continue
                if mask >> i & 1 and (g.length, g.qty) not in seen:
                    seen.add((g.length, g.qty))
                    out.append(i)
            v = tuple(out)
            self._cands_cache[mask] = v
        return v

    def _future_changes(self, mask: int, cur_len: int) -> int:
        n = self._nlen_cache.get(mask)
        if n is None:
            n = self._nlen_cache[mask] = sum(1 for _, lm in self.len_masks if mask & lm)
        # la misura corrente, se ancora presente, non richiede un cambio
        return n - (1 if mask & self.len_of[cur_len] else 0)

    def _reach(self, mask: int) -> int:
        """Bitset delle somme ottenibili con i pezzi (misura + kerf) dei gruppi in `mask`,
        con ripetizione illimitata (rilassamento: e' un limite valido per ogni pannello)."""
        v = self._reach_mask_cache.get(mask)
        if v is not None:
            return v
        lens = tuple(L for L, lm in self.len_masks if mask & lm)
        v = self._reach_cache.get(lens)
        if v is None:
            C, k = self.inst.capacity, self.inst.kerf
            full = (1 << (C + 1)) - 1
            v = 1
            for L in lens:
                step = L + k
                while step <= C:
                    v |= (v << step) & full
                    step <<= 1
            self._reach_cache[lens] = v
        self._reach_mask_cache[mask] = v
        return v

    @staticmethod
    def _waste_lb(r: int, reach: int) -> int:
        """Sfrido minimo garantito di un residuo r: r meno la piu' grande somma ottenibile <= r."""
        return r - ((reach & ((2 << r) - 1)).bit_length() - 1)

    # ---- espansione ---------------------------------------------------------------------
    def _children(self, st: _St, t: int, out: list):
        inst = self.inst
        k, S, C, lam = inst.kerf, self.slots, inst.capacity, inst.change_weight
        cands = self._cands(st.rem) if st.left == 0 else (st.cur,)
        mac = st.mac
        for gi in cands:
            g = self.groups[gi]
            L = g.length
            need = L + k
            if st.left == 0:
                rem2, left2 = st.rem & ~(1 << gi), g.qty - 1
            else:
                rem2, left2 = st.rem, st.left - 1
            piece_k = g.qty - left2
            change = st.m != L
            # misura piu' lunga dopo una piu' corta: violazione dell'ordine decrescente
            asc2 = st.asc + (1 if st.left == 0 and st.m != 0 and L > st.m else 0)
            nch2 = st.nch + (1 if change and st.m != 0 else 0)
            fut = self._minlen(rem2)
            if left2 > 0:
                fut = min(fut, L)
            final = fut >= INF
            thr_after = INF if final else fut + k   # residuo minimo utile dopo questo taglio
            thr_now = min(L, fut) + k               # residuo minimo utile per il pannello uscente

            opts = []
            if mac is not None and mac[0] >= need:
                opts.append(("A", -1, -1))
            prev = None
            n_sb = len(st.sb)
            for j, e in enumerate(st.sb):
                if e[0] < need:
                    break
                if e[0] != prev:
                    # residuo piu' piccolo tra gli altri pannelli in standby (candidato a essere chiuso)
                    other = st.sb[-1][0] if j != n_sb - 1 else (st.sb[-2][0] if n_sb >= 2 else None)
                    for md in self._out_modes(mac, thr_now, n_sb, S, other):
                        opts.append(("R", e[1], md))
                    prev = e[0]
            for md in self._out_modes(mac, thr_now, n_sb, S, st.sb[-1][0] if n_sb else None):
                opts.append(("N", -1, md))

            for kind, tpid, md in opts:
                ev = st.ev
                evs = []
                z2, z3a, z3b, z3c = st.z2, st.z3a, st.z3b, st.z3c
                wl = list(st.wl)     # scarti dei pannelli completati
                sb = list(st.sb)
                npan = st.npan
                if kind == "A":
                    if change:
                        ev += 1
                        evs.append(("CAMBIO_MISURA", L))
                    pid = mac[1]
                    newmac = (mac[0] - need, pid, mac[2], t)
                else:
                    target = None
                    if kind == "R":
                        for j, e in enumerate(sb):
                            if e[1] == tpid:
                                target = sb.pop(j)
                                break
                    if mac is not None:
                        ev += 1
                        if md == 2:
                            # il pannello in standby col residuo minore non verra' piu' ripreso:
                            # in ricostruzione il suo STANDBY diventa COMPLETE e libera il posto
                            d = sb.pop()
                            z3a += d[3] - d[2]
                            z3c += d[0] >= self.lmin_k
                            wl.append(d[0])
                        if md != 1:
                            evs.append(("STANDBY", mac[1]))
                            sb.append((mac[0], mac[1], mac[2], mac[3], ev))
                        else:
                            evs.append(("COMPLETE", mac[1]))
                            z3a += mac[3] - mac[2]
                            z3c += mac[0] >= self.lmin_k
                            wl.append(mac[0])
                    if change:
                        ev += 1
                        evs.append(("CAMBIO_MISURA", L))
                    if kind == "R":
                        ev += 1
                        pid = target[1]
                        evs.append(("RESUME", pid))
                        z2 += target[0] * (ev - target[4])
                        z3b += 1
                        newmac = (target[0] - need, pid, target[2], t)
                    else:
                        npan += 1
                        pid = npan
                        ev += 1
                        evs.append(("OPEN", pid))
                        newmac = (C - need, pid, t, t)
                ev += 1
                evs.append(("CUT", pid, gi, piece_k))

                # pannelli in standby diventati inutili: completati (retroattivamente)
                keep = []
                for e in sb:
                    if e[0] >= thr_after:
                        keep.append(e)
                    else:
                        z3a += e[3] - e[2]
                        z3c += e[0] >= self.lmin_k
                        wl.append(e[0])
                if final:
                    ev += 1
                    evs.append(("COMPLETE", newmac[1]))
                    z3a += newmac[3] - newmac[2]
                    z3c += newmac[0] >= self.lmin_k
                    wl.append(newmac[0])
                    newmac = None
                keep.sort(key=lambda e: (-e[0], e[1]))

                c = _St()
                c.rem, c.cur, c.left, c.m = rem2, gi, left2, L
                c.mac, c.sb = newmac, tuple(keep)
                c.npan, c.nch, c.ev = npan, nch2, ev
                c.z2, c.z3a, c.z3b, c.z3c = z2, z3a, z3b, z3c
                c.asc = asc2
                c.wl = tuple(wl)
                c.need = st.need - need
                c.parent, c.evs = st, tuple(evs)
                macr = newmac[0] if newmac is not None and newmac[0] >= thr_after else -1
                c.prof = (macr, tuple(e[0] for e in keep))
                # rango lessicografico (tie-break): prefisso del genitore + azione corrente
                c.tb = (st.tb, (g.inf_ord, g.gid, piece_k, pid, md, kind))
                # stima ammissibile del costo finale (Obiettivo 1)
                avail = max(macr, 0) + sum(c.prof[1])
                wlb = 0
                if not final:
                    reach = self._reach(rem2 | ((1 << gi) if left2 else 0))
                    if macr >= 0:
                        wlb += self._waste_lb(macr, reach)
                    for r in c.prof[1]:
                        wlb += self._waste_lb(r, reach)
                    usable_new = C - self._waste_lb(C, reach)
                else:
                    usable_new = C
                extra = c.need - (avail - wlb)
                lb_p = -(-extra // usable_new) if extra > 0 else 0
                z1 = C * npan + lam * nch2
                est = z1 + C * lb_p + lam * self._future_changes(rem2, L)
                pend2 = sum(e[0] * (ev - e[4]) for e in keep)
                pend3a = sum(t - e[2] for e in keep) + (t - newmac[2] if newmac else 0)
                # guida euristica: a parita' di stima, meno scarto gia' irrecuperabile
                # (pannelli chiusi + sfrido minimo garantito dei residui aperti)
                wc = C * npan - (self.need_total - c.need) - avail + wlb
                # avanzo: scarto chiuso nei pannelli diversi da quello con l'avanzo piu' lungo
                # (piccolo = scarto concentrato in un solo pannello, riutilizzabile)
                spread = sum(wl) - max(wl) if wl else 0
                c.rank = (est, wc, spread, asc2 if self.pref_desc else 0, z2 + pend2, z3a + pend3a, z3b, z3c)
                c.key = (rem2, gi if left2 else -1, left2, L)
                out.append((c, z1))
            self.nodes += len(opts)

    @staticmethod
    def _out_modes(mac, thr_now, n_sb, S, other):
        """Destino del pannello che esce dalla macchina: 0 = standby, 1 = completato,
        2 = standby chiudendo il pannello in standby col residuo minore `other` (posti pieni).
        `n_sb` conta anche l'eventuale pannello da riprendere: durante lo scambio sono a terra entrambi."""
        if mac is None:
            return (-1,)
        if mac[0] < thr_now:
            return (1,)
        if n_sb + 1 <= S:
            return (0, 1)
        if other is not None and mac[0] > other:
            return (2, 1)
        return (1,)

    # ---- ricerca ------------------------------------------------------------------------
    def solve(self) -> _St:
        root = _St()
        root.rem = (1 << len(self.groups)) - 1
        root.cur, root.left, root.m = -1, 0, 0
        root.mac, root.sb = None, ()
        root.npan = root.nch = 0
        root.ev = 0
        root.z2 = root.z3a = root.z3b = root.z3c = root.asc = 0
        root.wl = ()
        root.need = self.need_total
        root.parent, root.evs, root.tb = None, (), 0

        level = [root]
        N = self.inst.n_pieces
        for t in range(1, N + 1):
            children: list = []
            for st in level:
                self._children(st, t, children)
            # rango del tie-break: ordinamento totale (rango genitore, azione)
            children.sort(key=lambda cz: cz[0].tb)
            for r, (c, _) in enumerate(children):
                c.tb = r
            children.sort(key=lambda cz: (cz[0].rank, cz[0].tb))
            # dominanza a parita' di posizione
            buckets: dict = {}
            kept = []
            for c, z1 in children:
                lst = buckets.setdefault(c.key, [])
                mac_c, sb_c = c.prof
                asc_c = c.asc if self.pref_desc else 0
                dominated = False
                for z1k, asc_k, mac_k, sb_k in lst:
                    if (z1k <= z1 and asc_k <= asc_c and mac_k >= mac_c and len(sb_k) == len(sb_c)
                            and all(a >= b for a, b in zip(sb_k, sb_c))
                            and (z1k < z1 or asc_k < asc_c or (mac_k == mac_c and sb_k == sb_c))):
                        dominated = True
                        break
                if dominated:
                    continue
                lst.append((z1, asc_c, mac_c, sb_c))
                kept.append(c)
            if len(kept) > self.width:
                self.truncated = True
                self.truncated_levels += 1
                kept = kept[: self.width]
            for c in kept:   # libera i riferimenti non piu' necessari
                c.key = c.prof = None
            level = kept
            if self.progress:
                self.progress(t)

        lam, C = self.inst.change_weight, self.inst.capacity
        return min(level, key=lambda s: (C * s.npan + lam * s.nch, _leftover_key(s.wl),
                                         s.asc if self.pref_desc else 0, s.z2, s.z3a, s.z3b, s.z3c, s.tb))


def _reconstruct(inst: Instance, best: _St) -> list[dict]:
    chain = []
    s = best
    while s is not None:
        chain.append(s.evs)
        s = s.parent
    raw = [e for evs in reversed(chain) for e in evs]
    # Il primo setup della macchina e' un CAMBIO_MISURA marcato come setup iniziale.
    # Un pannello messo in standby e mai ripreso viene in realta' completato all'uscita.
    last_cut = {}
    for i, e in enumerate(raw):
        if e[0] == "CUT":
            last_cut[e[1]] = i
    events = []
    first_setup = True
    for i, e in enumerate(raw):
        tipo = e[0]
        if tipo == "STANDBY" and last_cut[e[1]] < i:
            tipo = "COMPLETE"
        d: dict = {"n": len(events) + 1, "tipo": tipo}
        if tipo == "CAMBIO_MISURA":
            d["misura"] = mm_to_cm(e[1])
            d["setup_iniziale"] = first_setup
            first_setup = False
        elif tipo == "CUT":
            g = inst.groups[e[2]]
            d.update({
                "pannello": f"P{e[1]}",
                "id_pezzo": f"{g.gid}.{e[3]:02d}",
                "group_id": g.gid,
                "id_infisso": g.inf_id,
                "misura": mm_to_cm(g.length),
            })
        else:
            d["pannello"] = f"P{e[1]}"
        events.append(d)
    return events


def solution_hash(parametri: dict, events: list[dict]) -> str:
    payload = json.dumps({"parametri": parametri, "eventi": events},
                         sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _tb_key(inst: Instance, events: list[dict]) -> tuple:
    """Chiave del tie-break finale: sequenza dei CUT come (ordine infisso, group_id, id_pezzo, n. pannello)."""
    order = {g.gid: g.inf_ord for g in inst.groups}
    return tuple((order[e["group_id"]], e["group_id"], e["id_pezzo"], int(e["pannello"][1:]))
                 for e in events if e["tipo"] == "CUT")


def solve(inst: Instance, level: str = "standard", width: int | None = None,
          progress: Callable[[float], None] | None = None) -> dict:
    """Risolve l'istanza e restituisce la soluzione come dizionario JSON-serializzabile.

    Portafoglio deterministico: la ricerca viene eseguita con 0, 1, ..., S posti di appoggio
    (una soluzione con meno posti e' ammissibile anche con S) e si tiene la migliore secondo
    l'intera gerarchia (Z1, avanzo piu' lungo, ordine misure, Z2, Z3a, Z3b, Z3c, tie-break).
    Garantisce che liberare un posto non peggiori mai il risultato. Si esegue tutto il portafoglio
    (anche quando Z1 e' gia' ottimo, ricerche con piu' posti concentrano meglio lo scarto); ci si
    ferma prima solo se nulla puo' piu' migliorare: Z1 al limite inferiore, tutto lo scarto in un
    solo pannello e, in modalita' "preferito", ordine perfettamente decrescente.
    `progress` riceve la frazione completata in [0, 1].
    """
    w = width if width is not None else _level_width(level, inst.n_pieces)
    lb = lower_bounds(inst)
    C, lam = inst.capacity, inst.change_weight
    S = inst.standby_slots
    N = inst.n_pieces
    best = best_key = best_events = None
    runs = []
    nodes = 0
    for cap in range(S + 1):
        cb = (lambda t, cap=cap: progress((cap * N + t) / ((S + 1) * N))) if progress else None
        sv = Solver(inst, w, cap, cb)
        st = sv.solve()
        nodes += sv.nodes
        events = _reconstruct(inst, st)
        z1 = C * st.npan + lam * st.nch
        key = (z1, _leftover_key(st.wl), st.asc if inst.ordine_misure == "preferito" else 0,
               st.z2, st.z3a, st.z3b, st.z3c, _tb_key(inst, events))
        runs.append({"posti_usati_nella_ricerca": cap, "pannelli": st.npan, "cambi_misura": st.nch,
                     "livelli_troncati": sv.truncated_levels})
        # a parita' di chiave vince la ricerca con meno posti (gia' in `best`)
        if best_key is None or key < best_key:
            best, best_key, best_events, best_sv = st, key, events, sv
        if (z1 == lb["z1"] and sum(st.wl) == max(st.wl)
                and (inst.ordine_misure != "preferito" or st.asc == 0)):
            break
    if progress:
        progress(1.0)
    events = best_events
    sv = best_sv
    z1 = best_key[0]
    const = sum((g.length + inst.kerf) * g.qty for g in inst.groups)
    # esaustivita' vale solo per la ricerca col numero pieno di posti
    exhaustive = len(runs) == S + 1 and runs[-1]["livelli_troncati"] == 0
    proven = exhaustive or z1 == lb["z1"]
    return {
        "parametri": inst.raw_params,
        "gruppi": [
            {"group_id": g.gid, "id_infisso": g.inf_id, "ordine_infisso": g.inf_ord,
             "misura": mm_to_cm(g.length), "quantita": g.qty}
            for g in inst.groups
        ],
        "eventi": events,
        "hash": solution_hash(inst.raw_params, events),
        "solver": {
            "metodo": "DP con dominanza + beam search deterministico (portafoglio su 0..S posti)",
            "livello": level if width is None else "personalizzato",
            "ampiezza_beam": w,
            "nodi_generati": nodes,
            "ricerche": runs,
            "certificazione_obiettivo_1": "OTTIMO_PROVATO" if proven else "EURISTICO",
            "motivo_certificazione": (
                "costo uguale al limite inferiore" if z1 == lb["z1"]
                else ("ricerca esaustiva (nessun troncamento)"
                      + (", tra le soluzioni con misure in ordine decrescente"
                         if inst.ordine_misure == "decrescente" else "")) if proven
                else "beam troncato; gap rispetto al limite inferiore riportato"
            ),
            "obiettivi_2_3": "ottimizzati a parita' di obiettivo 1 (euristici se il beam tronca)",
            "limite_inferiore": {
                "pannelli": lb["pannelli"],
                "cambi": lb["cambi"],
                "costo_cm": mm_to_cm(lb["z1"] - const),
            },
            "gap_costo_cm": mm_to_cm(z1 - lb["z1"]),
        },
        "dichiarato_dal_solver": {
            "pannelli": best.npan,
            "cambi_misura": best.nch,
            "scarto_totale_cm": mm_to_cm(C * best.npan - const),
            "costo_cm": mm_to_cm(z1 - const),
            "obiettivo_2": mm_to_cm(best.z2),
            "obiettivo_3a": best.z3a,
            "obiettivo_3b": best.z3b,
            "obiettivo_3c": best.z3c,
            "inversioni_ordine_misure": best.asc,
            "avanzo_massimo_cm": mm_to_cm(max(best.wl)),
        },
    }
