#report.py
"""Piano di taglio generato automaticamente dalla soluzione (Fase 5, par. 11).

Tutto e' ricavato SOLO dalla sequenza di eventi della soluzione (e dai valori ricalcolati
dal validatore per il riepilogo), quindi grafico, composizione, sequenza, quantita' e scarto
sono coerenti per costruzione. Uscite:
  - render_fragment(sol): sezioni A-F in HTML (usato dalla pagina web)
  - render_standalone(sol, css): documento HTML completo, scaricabile/stampabile
  - render_text(sol): piano in testo semplice (fallback)

Grafico di ogni pannello (par. 11.2): barra proporzionale ai cm reali, colore per misura
(misure in ordine decrescente sulla palette; oltre 8 colori si ricicla con tratteggio), kerf
come linea scura sottile, scarto grigio tratteggiato. Ogni segmento riporta misura, numero
d'ordine di taglio, group_id e blocco; se e' piu' stretto di 60 px mostra solo il numero
d'ordine e i dettagli vanno nella legenda sotto il grafico.
"""
from __future__ import annotations

from html import escape

PALETTE = ["#1f77b4", "#2ca02c", "#e6b800", "#ff7f0e", "#9467bd", "#17becf", "#d62728", "#8c564b"]
_LIGHT = {"#e6b800", "#17becf", "#ff7f0e", "#2ca02c"}   # colori su cui il testo va scuro

BAR_W = 1000          # px della barra di un pannello (lunghezza nominale)
BAR_H = 56
PAD_X = 4
MIN_LABEL_W = 60      # soglia fissa (px) per le etichette complete
KERF_MIN_W = 1.5      # larghezza minima visibile del kerf (px)
WASTE_COL_W = 130     # colonna a destra della barra con lo scarto in cm (sempre visibile)


def _mm(cm) -> int:
    return round(float(cm) * 10)


def _fmt(mm: int) -> str:
    """mm interi -> testo in cm con al piu' 1 decimale."""
    s = f"{mm / 10:.1f}"
    return s[:-2] if s.endswith(".0") else s


def _circled(n: int) -> str:
    if 1 <= n <= 20:
        return chr(0x2460 + n - 1)
    if 21 <= n <= 35:
        return chr(0x3251 + n - 21)
    if 36 <= n <= 50:
        return chr(0x32B1 + n - 36)
    return f"({n})"


# ---------------------------------------------------------------------------------------
# Ricostruzione dalla sequenza di eventi
# ---------------------------------------------------------------------------------------
def analyze(sol: dict) -> dict:
    """Unica passata sugli eventi: pannelli, blocchi macchina, passi del piano operatore."""
    p = sol["parametri"]
    L, k, cap, S = _mm(p["lunghezza_pannello"]), _mm(p["kerf"]), _mm(p["capacita_utile"]), p["posti_appoggio"]
    panels: dict[str, dict] = {}
    order: list[str] = []
    blocks: list[dict] = []
    state: dict[str, str] = {}
    cut_n = 0
    step_n = 0

    def standby_now() -> list[str]:
        return [q for q in order if state[q] == "STANDBY"]

    for e in sol["eventi"]:
        t, pid = e["tipo"], e.get("pannello")
        if t == "CAMBIO_MISURA":
            blocks.append({"n": len(blocks) + 1, "misura": _mm(e["misura"]), "passi": [],
                           "pannelli": [], "standby_inizio": standby_now(), "setup": e["setup_iniziale"]})
        blk = blocks[-1]
        step_n += 1
        if t == "CAMBIO_MISURA":
            txt = (f"IMPOSTA MISURA → {_fmt(blk['misura'])} cm" if blk["setup"]
                   else f"CAMBIA MISURA → {_fmt(blk['misura'])} cm")
            blk["passi"].append({"n": step_n, "cls": "s-change", "testo": txt, "nota": ""})
        elif t == "OPEN":
            panels[pid] = {"id": pid, "tagli": [], "used": 0, "standby": 0, "stato": "IN_MACCHINA"}
            order.append(pid)
            state[pid] = "IN_MACCHINA"
            blk["passi"].append({"n": step_n, "cls": "s-open",
                                 "testo": f"APRI {pid}", "nota": f"pannello nuovo da {_fmt(L)} cm"})
        elif t == "CUT":
            cut_n += 1
            pan = panels[pid]
            m = _mm(e["misura"])
            pan["used"] += m + k
            c = {"n": len(pan["tagli"]) + 1, "passo": cut_n, "misura": m, "group_id": e["group_id"],
                 "id_infisso": e["id_infisso"], "id_pezzo": e["id_pezzo"], "blocco": blk["n"]}
            pan["tagli"].append(c)
            if pid not in blk["pannelli"]:
                blk["pannelli"].append(pid)
            blk["passi"].append({
                "n": step_n, "cls": "s-cut",
                "testo": f"TAGLIA {pid} → {_fmt(m)} cm ({e['group_id']}, infisso {e['id_infisso']})",
                "nota": f"pezzo {e['id_pezzo']} · {_circled(c['n'])} su {pid} · residuo {_fmt(cap - pan['used'])} cm"})
        elif t == "STANDBY":
            state[pid] = "STANDBY"
            panels[pid]["standby"] += 1
            blk["passi"].append({
                "n": step_n, "cls": "s-standby", "testo": f"METTI {pid} IN STANDBY",
                "nota": f"residuo {_fmt(cap - panels[pid]['used'])} cm · appoggi occupati {len(standby_now())}/{S}"})
        elif t == "RESUME":
            state[pid] = "IN_MACCHINA"
            blk["passi"].append({
                "n": step_n, "cls": "s-resume", "testo": f"RIPRENDI {pid}",
                "nota": f"dallo standby, residuo {_fmt(cap - panels[pid]['used'])} cm · "
                        f"appoggi occupati {len(standby_now())}/{S}"})
        elif t == "COMPLETE":
            state[pid] = "COMPLETATO"
            panels[pid]["stato"] = "COMPLETATO"
            blk["passi"].append({"n": step_n, "cls": "s-complete", "testo": f"COMPLETA {pid}",
                                 "nota": f"scarto {_fmt(cap - panels[pid]['used'])} cm"})
        blk["standby_fine"] = standby_now()

    out_panels = []
    for pid in order:
        pan = panels[pid]
        pan["scarto"] = cap - pan["used"]
        pan["kerf_tot"] = k * len(pan["tagli"])
        bl: list[int] = []
        for c in pan["tagli"]:
            if c["blocco"] not in bl:
                bl.append(c["blocco"])
        pan["blocchi"] = bl
        dest: dict[str, list[str]] = {}
        for c in pan["tagli"]:
            gs = dest.setdefault(c["id_infisso"], [])
            if c["group_id"] not in gs:
                gs.append(c["group_id"])
        pan["dest"] = dest
        out_panels.append(pan)
    return {"L": L, "k": k, "cap": cap, "S": S, "panels": out_panels, "blocks": blocks}


def color_map(sol: dict) -> dict[int, tuple[str, bool]]:
    """Misura (mm) -> (colore, tratteggio). Misure in ordine decrescente."""
    misure = sorted({_mm(e["misura"]) for e in sol["eventi"] if e["tipo"] == "CUT"}, reverse=True)
    return {m: (PALETTE[i % len(PALETTE)], i >= len(PALETTE)) for i, m in enumerate(misure)}


# ---------------------------------------------------------------------------------------
# SVG
# ---------------------------------------------------------------------------------------
def _defs(uid: str) -> str:
    return (
        '<defs>'
        f'<pattern id="hw-{uid}" width="8" height="8" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">'
        '<rect width="8" height="8" fill="#d9d9d9"/><line x1="0" y1="0" x2="0" y2="8" stroke="#9a9a9a" stroke-width="3"/></pattern>'
        f'<pattern id="hm-{uid}" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(-45)">'
        '<rect width="6" height="6" fill="#bdbdbd"/><line x1="0" y1="0" x2="0" y2="6" stroke="#7a7a7a" stroke-width="2"/></pattern>'
        f'<pattern id="hr-{uid}" width="10" height="10" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">'
        '<line x1="0" y1="0" x2="0" y2="10" stroke="#ffffff" stroke-opacity=".55" stroke-width="3"/></pattern>'
        '</defs>'
    )


def _bar(pan: dict, an: dict, colors: dict, x0: float, y: float, w_total: float, h: float,
         uid: str, labels: bool) -> tuple[list[str], list[dict]]:
    """Segmenti di un pannello come elementi SVG. Restituisce (parti, segmenti stretti)."""
    sx = w_total / an["L"]
    parts: list[str] = []
    small: list[dict] = []
    x = x0
    for c in pan["tagli"]:
        w = c["misura"] * sx
        col, hatch = colors[c["misura"]]
        tip = (f'Taglio {c["n"]} su {pan["id"]} (passo {c["passo"]}): {_fmt(c["misura"])} cm, '
               f'gruppo {c["group_id"]}, infisso {c["id_infisso"]}, blocco B{c["blocco"]}')
        parts.append(f'<g><title>{escape(tip)}</title><rect x="{x:.2f}" y="{y:.2f}" width="{w:.2f}" height="{h}" fill="{col}"/>')
        if hatch:
            parts.append(f'<rect x="{x:.2f}" y="{y:.2f}" width="{w:.2f}" height="{h}" fill="url(#hr-{uid})"/>')
        tc = "#1b1b1b" if col in _LIGHT else "#ffffff"
        cx = x + w / 2
        if labels:
            if w >= MIN_LABEL_W:
                parts.append(
                    f'<text x="{cx:.2f}" y="{y + 17:.2f}" text-anchor="middle" fill="{tc}" font-size="13" font-weight="700">'
                    f'{_circled(c["n"])} {_fmt(c["misura"])}</text>'
                    f'<text x="{cx:.2f}" y="{y + 33:.2f}" text-anchor="middle" fill="{tc}" font-size="11">{escape(c["group_id"])}</text>'
                    f'<text x="{cx:.2f}" y="{y + 48:.2f}" text-anchor="middle" fill="{tc}" font-size="10" opacity=".9">B{c["blocco"]}</text>')
            else:
                small.append(c)
                parts.append(f'<text x="{cx:.2f}" y="{y + h / 2 + 5:.2f}" text-anchor="middle" fill="{tc}" '
                             f'font-size="13" font-weight="700">{_circled(c["n"])}</text>')
        elif w >= 40:
            parts.append(f'<text x="{cx:.2f}" y="{y + h / 2 + 5:.2f}" text-anchor="middle" fill="{tc}" '
                         f'font-size="14" font-weight="700">{_fmt(c["misura"])}</text>')
        parts.append('</g>')
        x += w
        kw = max(an["k"] * sx, KERF_MIN_W)
        parts.append(f'<rect x="{x:.2f}" y="{y:.2f}" width="{kw:.2f}" height="{h}" fill="#202020">'
                     f'<title>kerf {_fmt(an["k"])} cm</title></rect>')
        x += an["k"] * sx
    ws = pan["scarto"] * sx
    if ws > 0.05:
        parts.append(f'<g><title>Scarto {_fmt(pan["scarto"])} cm</title>'
                     f'<rect x="{x:.2f}" y="{y:.2f}" width="{ws:.2f}" height="{h}" fill="url(#hw-{uid})"/></g>')
        x += ws
    wm = (an["L"] - an["cap"]) * sx
    if wm > 0.05:
        parts.append(f'<rect x="{x:.2f}" y="{y:.2f}" width="{wm:.2f}" height="{h}" fill="url(#hm-{uid})">'
                     f'<title>Margini e zone non tagliabili {_fmt(an["L"] - an["cap"])} cm</title></rect>')
    parts.append(f'<rect x="{x0:.2f}" y="{y:.2f}" width="{w_total:.2f}" height="{h}" class="panel-border" rx="2"/>')
    # scarto in cm, sempre leggibile a destra della barra
    tx = x0 + w_total + 12
    if labels:
        parts.append(f'<text x="{tx:.2f}" y="{y + h / 2 - 2:.2f}" font-size="20" font-weight="700" class="waste-lbl">'
                     f'{_fmt(pan["scarto"])} cm</text>'
                     f'<text x="{tx:.2f}" y="{y + h / 2 + 16:.2f}" font-size="12" class="waste-sub">scarto</text>')
    else:
        parts.append(f'<text x="{tx:.2f}" y="{y + h / 2 + 6:.2f}" font-size="17" font-weight="700" class="waste-lbl">'
                     f'{_fmt(pan["scarto"])} cm</text>')
    return parts, small


def panel_svg(pan: dict, an: dict, colors: dict) -> tuple[str, list[dict]]:
    uid = pan["id"]
    top = 4
    parts = [f'<svg class="panel-svg" viewBox="0 0 {BAR_W + 2 * PAD_X + WASTE_COL_W} {BAR_H + 2 * top}" width="100%" role="img" '
             f'aria-label="Pannello {escape(pan["id"])}" xmlns="http://www.w3.org/2000/svg">', _defs(uid),
             f'<rect x="{PAD_X}" y="{top}" width="{BAR_W}" height="{BAR_H}" class="panel-bg" rx="2"/>']
    bar, small = _bar(pan, an, colors, PAD_X, top, BAR_W, BAR_H, uid, labels=True)
    parts += bar
    parts.append('</svg>')
    return "".join(parts), small


def global_svg(an: dict, colors: dict) -> str:
    """Vista globale: tutti i pannelli, una riga ciascuno, stessa scala."""
    row_h, gap, lab_w, head = 28, 8, 52, 22
    n = len(an["panels"])
    H = head + n * (row_h + gap) + gap
    W = lab_w + BAR_W + PAD_X + WASTE_COL_W
    parts = [f'<svg class="global-svg" viewBox="0 0 {W} {H}" width="100%" role="img" '
             f'aria-label="Vista globale dei pannelli" xmlns="http://www.w3.org/2000/svg">', _defs("glob"),
             f'<text x="{lab_w + BAR_W + 12}" y="17" font-size="15" class="waste-sub">scarto</text>']
    for i, pan in enumerate(an["panels"]):
        y = head + gap + i * (row_h + gap)
        parts.append(f'<text x="{lab_w - 8}" y="{y + row_h / 2 + 6}" text-anchor="end" font-size="16" '
                     f'font-weight="700" class="lbl">{escape(pan["id"])}</text>')
        parts.append(f'<rect x="{lab_w}" y="{y}" width="{BAR_W}" height="{row_h}" class="panel-bg"/>')
        bar, _ = _bar(pan, an, colors, lab_w, y, BAR_W, row_h, "glob", labels=False)
        parts += bar
    parts.append('</svg>')
    return "".join(parts)


# ---------------------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------------------
def _legend(colors: dict, an: dict) -> str:
    items = "".join(
        f'<span class="lg"><i style="background:{c}" class="{"rec" if h else ""}"></i>{_fmt(m)} cm</span>'
        for m, (c, h) in colors.items())
    items += '<span class="lg"><i class="waste"></i>scarto</span><span class="lg"><i class="kerf"></i>kerf</span>'
    if an["L"] != an["cap"]:
        items += '<span class="lg"><i class="margin"></i>margini / zone non tagliabili</span>'
    return f'<div class="legend">{items}</div>'


def _summary(sol: dict, an: dict) -> str:
    r = sol["validazione"]["ricalcolato"]
    sv = sol["solver"]
    p = sol["parametri"]
    proven = sv["certificazione_obiettivo_1"] == "OTTIMO_PROVATO"
    kpi = lambda v, l: f'<div class="kpi"><div class="v">{escape(str(v))}</div><div class="l">{escape(l)}</div></div>'
    ordini = {"preferito": "decrescente se non costa sfrido", "decrescente": "decrescente (rigido)", "libero": "libero"}
    return f'''<section class="card">
  <h2>A. Riepilogo
    <span class="badge {"ok" if proven else "warn"}">{"pannelli e cambi: ottimo provato" if proven else "euristico"}</span>
    <span class="badge ok">validato ✓</span></h2>
  <div class="kpis">
    {kpi(r["pannelli"], "pannelli usati")}
    {kpi(r["cambi_misura"], "cambi misura")}
    {kpi(f'{r["scarto_totale_cm"]} cm', "scarto totale")}
    {kpi(f'{r["standby_massimo"]} / {r["posti_appoggio"]}', "standby massimo / posti")}
    {kpi(f'{r["avanzo_massimo_cm"]} cm', f'avanzo più lungo ({r["avanzo_massimo_pannello"]})')}
    {kpi(f'{r["costo_cm"]} cm', "costo (scarto + peso × cambi)")}
  </div>
  <p class="small muted">Barra {_fmt(_mm(p["lunghezza_pannello"]))} cm · margini {_fmt(_mm(p["margini"]))} cm ·
    zone non tagliabili {_fmt(_mm(p["zone_non_tagliabili"]))} cm · capacità utile {_fmt(_mm(p["capacita_utile"]))} cm ·
    kerf {_fmt(_mm(p["kerf"]))} cm · posti di appoggio {p["posti_appoggio"]} · peso cambio {_fmt(_mm(p["scarto_extra_max_per_cambio"]))} cm ·
    ordine misure: {escape(ordini.get(p["ordine_misure"], p["ordine_misure"]))}</p>
  <p class="small muted">Limite inferiore: {sv["limite_inferiore"]["pannelli"]} pannelli, {sv["limite_inferiore"]["cambi"]} cambi ·
    distanza dal limite {sv["gap_costo_cm"]} cm · {escape(sv["motivo_certificazione"])}</p>
  <p class="small muted">Hash soluzione: <span class="mono">{escape(sol["hash"])}</span></p>
</section>'''


def _operator(an: dict) -> str:
    out = ['<section class="card"><h2>C. Piano per l\'operatore</h2>',
           '<p class="small muted">Passi in ordine. Ogni blocco è una impostazione della macchina su una misura.</p>']
    for b in an["blocks"]:
        sb_i = ", ".join(b["standby_inizio"]) or "nessuno"
        sb_f = ", ".join(b["standby_fine"]) or "nessuno"
        out.append(f'''<div class="block"><div class="block-head">
  <b>Blocco B{b["n"]} · misura {_fmt(b["misura"])} cm</b>
  <span>pannelli lavorati: {escape(", ".join(b["pannelli"]))}</span>
  <span class="muted">in standby all'inizio: {escape(sb_i)} · alla fine: {escape(sb_f)}</span></div>
  <ol class="steps">''')
        for s in b["passi"]:
            nota = f' <span class="tag">— {escape(s["nota"])}</span>' if s["nota"] else ""
            out.append(f'<li value="{s["n"]}" class="{s["cls"]}">{escape(s["testo"])}{nota}</li>')
        out.append('</ol></div>')
    out.append('</section>')
    return "".join(out)


def _panel_details(an: dict, colors: dict) -> str:
    out = ['<section class="card"><h2>D. Piano dettagliato per pannello</h2>', _legend(colors, an),
           '<p class="small muted">Su ogni segmento: ① numero d\'ordine di taglio sul pannello, misura, gruppo, '
           'blocco macchina. Larghezze proporzionali ai cm reali.</p>']
    for pan in an["panels"]:
        svg, small = panel_svg(pan, an, colors)
        det = ""
        if small:
            det = '<ul class="small-det">' + "".join(
                f'<li><b>{_circled(c["n"])}</b> {_fmt(c["misura"])} cm · {escape(c["group_id"])} · '
                f'infisso {escape(c["id_infisso"])} · B{c["blocco"]}</li>' for c in small) + '</ul>'
        comp = "; ".join(f'{_circled(c["n"])} {_fmt(c["misura"])} ({c["group_id"]}, inf. {c["id_infisso"]}, B{c["blocco"]})'
                         for c in pan["tagli"])
        dest = "; ".join(f"Infisso {i} → {', '.join(g)}" for i, g in pan["dest"].items())
        misure = " + ".join(_fmt(c["misura"]) for c in pan["tagli"])
        out.append(f'''<div class="panel">
  <div class="panel-head"><b>{escape(pan["id"])}</b>
    <span>{escape(misure)} · scarto <b>{_fmt(pan["scarto"])} cm</b></span>
    <span class="muted">blocchi {" → ".join(f"B{b}" for b in pan["blocchi"])}{f' · standby ×{pan["standby"]}' if pan["standby"] else ""} · {pan["stato"].lower()}</span></div>
  {svg}{det}
  <dl class="panel-meta">
    <dt>Lunghezza</dt><dd>{_fmt(an["L"])} cm (utile {_fmt(an["cap"])} cm)</dd>
    <dt>Ordine di taglio</dt><dd>{escape(comp)}</dd>
    <dt>Scarto / kerf</dt><dd>{_fmt(pan["scarto"])} cm / {_fmt(pan["kerf_tot"])} cm</dd>
    <dt>Destinazione</dt><dd>{escape(dest)}</dd>
    <dt>Stato finale</dt><dd>{pan["stato"]}</dd>
  </dl>
</div>''')
    out.append('</section>')
    return "".join(out)


def _groups_table(sol: dict, an: dict) -> str:
    where: dict[str, dict[str, int]] = {}
    blk: dict[str, list[int]] = {}
    # in ordine di taglio, cosi' i pannelli di ogni gruppo compaiono nell'ordine reale
    cuts = sorted(((c, pan["id"]) for pan in an["panels"] for c in pan["tagli"]), key=lambda x: x[0]["passo"])
    for c, pid in cuts:
        d = where.setdefault(c["group_id"], {})
        d[pid] = d.get(pid, 0) + 1
        bl = blk.setdefault(c["group_id"], [])
        if c["blocco"] not in bl:
            bl.append(c["blocco"])
    rows = []
    for g in sol["gruppi"]:
        gid = g["group_id"]
        pans = ", ".join(f"{p}×{n}" for p, n in where.get(gid, {}).items())
        rows.append(f'<tr><td><b>{escape(gid)}</b></td><td>{escape(g["id_infisso"])}</td>'
                    f'<td class="num">{_fmt(_mm(g["misura"]))}</td><td class="num">{g["quantita"]}</td>'
                    f'<td>{escape(pans)}</td><td>{", ".join(f"B{b}" for b in blk.get(gid, []))}</td></tr>')
    return ('<section class="card"><h2>E. Tabella gruppi</h2><div class="table-wrap"><table>'
            '<thead><tr><th>group_id</th><th>infisso</th><th class="num">misura (cm)</th><th class="num">quantità</th>'
            '<th>pannelli</th><th>blocco</th></tr></thead><tbody>' + "".join(rows) + '</tbody></table></div></section>')


def _panels_table(an: dict) -> str:
    rows = []
    for pan in an["panels"]:
        tagli = ", ".join(_fmt(c["misura"]) for c in pan["tagli"])
        gruppi = ", ".join(dict.fromkeys(c["group_id"] for c in pan["tagli"]))
        infissi = ", ".join(dict.fromkeys(c["id_infisso"] for c in pan["tagli"]))
        rows.append(f'<tr><td><b>{escape(pan["id"])}</b></td><td>{escape(tagli)}</td><td>{escape(gruppi)}</td>'
                    f'<td>{escape(infissi)}</td><td>{" → ".join(f"B{b}" for b in pan["blocchi"])}</td>'
                    f'<td class="num">{_fmt(pan["scarto"])}</td><td>{pan["stato"]}</td></tr>')
    return ('<section class="card"><h2>F. Tabella pannelli</h2><div class="table-wrap"><table>'
            '<thead><tr><th>pannello</th><th>tagli presenti (cm, in ordine)</th><th>gruppi</th><th>infissi</th>'
            '<th>ordine blocchi</th><th class="num">scarto (cm)</th><th>stato</th></tr></thead><tbody>'
            + "".join(rows) + '</tbody></table></div></section>')


def render_fragment(sol: dict) -> str:
    """Sezioni A-F. Richiede sol["validazione"] (il piano si mostra solo se validato)."""
    an = analyze(sol)
    colors = color_map(sol)
    glob = (f'<section class="card"><h2>B. Vista globale</h2>{_legend(colors, an)}'
            f'{global_svg(an, colors)}</section>')
    return "".join([_summary(sol, an), glob, _operator(an), _panel_details(an, colors),
                    _groups_table(sol, an), _panels_table(an)])


def render_standalone(sol: dict, css: str) -> str:
    return f'''<!doctype html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Piano di taglio</title>
<style>
{css}
main {{ max-width: 1100px; margin: 0 auto; padding: 16px; }}
</style>
</head>
<body>
<main>
<h1>Piano di taglio</h1>
{render_fragment(sol)}
</main>
</body>
</html>
'''


def render_text(sol: dict) -> str:
    an = analyze(sol)
    r = sol["validazione"]["ricalcolato"]
    lines = [
        "PIANO DI TAGLIO",
        "=" * 60,
        f"Pannelli: {r['pannelli']}   Cambi misura: {r['cambi_misura']}   Scarto totale: {r['scarto_totale_cm']} cm",
        f"Standby massimo: {r['standby_massimo']}/{r['posti_appoggio']}   Costo: {r['costo_cm']} cm",
        f"Avanzo più lungo: {r['avanzo_massimo_cm']} cm su {r['avanzo_massimo_pannello']}",
        f"Hash: {sol['hash']}",
        "",
        "PIANO PER L'OPERATORE",
        "-" * 60,
    ]
    for b in an["blocks"]:
        lines.append(f"[Blocco B{b['n']} - misura {_fmt(b['misura'])} cm - pannelli: {', '.join(b['pannelli'])} - "
                     f"standby all'inizio: {', '.join(b['standby_inizio']) or 'nessuno'}]")
        for s in b["passi"]:
            lines.append(f"{s['n']:>4}. {s['testo']}" + (f"   ({s['nota']})" if s["nota"] else ""))
        lines.append("")
    lines += ["PANNELLI", "-" * 60]
    for pan in an["panels"]:
        comp = ", ".join(f"{c['n']}) {_fmt(c['misura'])} {c['group_id']} inf.{c['id_infisso']} B{c['blocco']}"
                         for c in pan["tagli"])
        lines.append(f"{pan['id']}: {comp} | scarto {_fmt(pan['scarto'])} cm | {pan['stato']}")
    return "\n".join(lines) + "\n"
