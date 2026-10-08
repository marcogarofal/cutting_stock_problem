#cpsat.py
"""Verificatore esatto CP-SAT per istanze piccole (Fase 2, par. 2.4-2.5 e 2.10).

Modello a posizioni, indipendente dalla DP del solver: serve a confermare il valore
ottimo dell'Obiettivo 1 (scarto + peso x cambi). Gli obiettivi 2 e 3 non sono modellati.

Determinismo: num_workers = 1, random_seed fisso, limite in tempo deterministico
(max_deterministic_time), mai in secondi reali.
"""
from __future__ import annotations

from ortools.sat.python import cp_model

from .model import Instance, mm_to_cm

MAX_PEZZI_CPSAT = 24


def verify_z1(inst: Instance, max_panels: int | None = None,
              deterministic_time: float = 60.0) -> dict:
    """Minimizza Z1 sul modello a posizioni. `max_panels` e' un limite superiore
    sul numero di pannelli (es. quello trovato dal solver); di default N pezzi."""
    N = inst.n_pieces
    if N > MAX_PEZZI_CPSAT:
        raise ValueError(f"CP-SAT usato solo come verifica su istanze fino a {MAX_PEZZI_CPSAT} pezzi.")
    C, k, S, lam = inst.capacity, inst.kerf, inst.standby_slots, inst.change_weight
    G = inst.groups
    P = max_panels or N
    T = range(N)
    m = cp_model.CpModel()

    # (K) contiguita': ogni gruppo e' un intervallo di posizioni, gli intervalli piastrellano [0, N)
    s = [m.NewIntVar(0, N - g.qty, f"s_{g.gid}") for g in G]
    m.AddNoOverlap([m.NewFixedSizeIntervalVar(s[i], g.qty, f"I_{g.gid}") for i, g in enumerate(G)])
    z = {}
    for i, g in enumerate(G):
        for t in T:
            b = z[i, t] = m.NewBoolVar(f"z_{i}_{t}")
            lo, hi = m.NewBoolVar(""), m.NewBoolVar("")
            m.Add(s[i] <= t).OnlyEnforceIf(lo)
            m.Add(s[i] > t).OnlyEnforceIf(lo.Not())
            m.Add(s[i] >= t - g.qty + 1).OnlyEnforceIf(hi)
            m.Add(s[i] < t - g.qty + 1).OnlyEnforceIf(hi.Not())
            m.AddBoolAnd([lo, hi]).OnlyEnforceIf(b)
            m.AddBoolOr([lo.Not(), hi.Not()]).OnlyEnforceIf(b.Not())
    for t in T:
        m.AddExactlyOne(z[i, t] for i in range(len(G)))
    # simmetria: gruppi identici (misura, quantita') in ordine di input
    for i in range(len(G)):
        for j in range(i + 1, len(G)):
            if (G[i].length, G[i].qty) == (G[j].length, G[j].qty):
                m.Add(s[i] < s[j])
            elif inst.ordine_misure == "decrescente" and G[i].length != G[j].length:
                a, b = (i, j) if G[i].length > G[j].length else (j, i)
                m.Add(s[a] < s[b])

    lens = sorted({g.length for g in G})
    ln = [m.NewIntVarFromDomain(cp_model.Domain.FromValues(lens), f"len_{t}") for t in T]
    for t in T:
        m.Add(ln[t] == sum(g.length * z[i, t] for i, g in enumerate(G)))

    # (A) assegnazione e (C) capacita'
    x = {(t, p): m.NewBoolVar(f"x_{t}_{p}") for t in T for p in range(P)}
    u = [m.NewBoolVar(f"u_{p}") for p in range(P)]
    for t in T:
        m.AddExactlyOne(x[t, p] for p in range(P))
    for p in range(P):
        q = []
        for t in T:
            m.AddImplication(x[t, p], u[p])
            v = m.NewIntVar(0, max(lens), "")
            m.Add(v == ln[t]).OnlyEnforceIf(x[t, p])
            m.Add(v == 0).OnlyEnforceIf(x[t, p].Not())
            q.append(v)
        m.Add(sum(q) + k * sum(x[t, p] for t in T) <= C)
        m.AddBoolOr([x[t, p] for t in T]).OnlyEnforceIf(u[p])
    for p in range(P - 1):
        m.AddImplication(u[p + 1], u[p])

    # (F) primo e ultimo taglio; id pannello = ordine di apertura
    f = [m.NewIntVar(0, N, f"f_{p}") for p in range(P)]
    last = [m.NewIntVar(0, N - 1, f"l_{p}") for p in range(P)]
    for p in range(P):
        m.AddMinEquality(f[p], [t + N - N * x[t, p] for t in T])
        m.AddMaxEquality(last[p], [t * x[t, p] for t in T])
    for p in range(P - 1):
        m.Add(f[p] < f[p + 1]).OnlyEnforceIf(u[p + 1])

    # (S) standby al confine t -> t+1: aperti (f <= t < l) meno l'eventuale pannello che resta in macchina
    for t in range(N - 1):
        same = m.NewBoolVar(f"same_{t}")
        for p in range(P):
            m.Add(x[t, p] == x[t + 1, p]).OnlyEnforceIf(same)
            m.AddBoolOr([x[t, p].Not(), x[t + 1, p].Not()]).OnlyEnforceIf(same.Not())
        opened = []
        for p in range(P):
            a, b1, b2 = m.NewBoolVar(""), m.NewBoolVar(""), m.NewBoolVar("")
            m.Add(f[p] <= t).OnlyEnforceIf(b1)
            m.Add(f[p] > t).OnlyEnforceIf(b1.Not())
            m.Add(last[p] > t).OnlyEnforceIf(b2)
            m.Add(last[p] <= t).OnlyEnforceIf(b2.Not())
            m.AddBoolAnd([b1, b2]).OnlyEnforceIf(a)
            m.AddBoolOr([b1.Not(), b2.Not()]).OnlyEnforceIf(a.Not())
            opened.append(a)
        m.Add(sum(opened) - same <= S)

    # (Mi) cambi misura (il primo setup non conta)
    ch = []
    for t in range(1, N):
        c = m.NewBoolVar(f"c_{t}")
        m.Add(ln[t] == ln[t - 1]).OnlyEnforceIf(c.Not())
        ch.append(c)

    const = sum((g.length + k) * g.qty for g in G)
    m.Minimize(C * sum(u) + lam * sum(ch))

    solver = cp_model.CpSolver()
    solver.parameters.num_workers = 1
    solver.parameters.random_seed = 0
    solver.parameters.max_deterministic_time = deterministic_time
    st = solver.Solve(m)
    if st not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return {"stato": solver.StatusName(st)}
    n_pan = sum(solver.Value(v) for v in u)
    n_ch = sum(solver.Value(v) for v in ch)
    z1 = C * n_pan + lam * n_ch
    return {
        "stato": "OTTIMO" if st == cp_model.OPTIMAL else "AMMISSIBILE",
        "pannelli": n_pan,
        "cambi_misura": n_ch,
        "scarto_totale_cm": mm_to_cm(C * n_pan - const),
        "costo_cm": mm_to_cm(z1 - const),
        "limite_inferiore_costo_cm": mm_to_cm(round(solver.BestObjectiveBound()) - const),
        "tempo_deterministico": round(solver.deterministic_time, 3),
    }
