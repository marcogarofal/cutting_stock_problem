"use strict";

const $ = (id) => document.getElementById(id);
const tbody = document.querySelector("#tbl tbody");
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const ESEMPIO = {
  parametri: { lunghezza_pannello: 650, kerf: 0.3, posti_appoggio: 2, scarto_extra_max_per_cambio: 20, margini: 15, zone_non_tagliabili: 0 },
  infissi: [
    { id_infisso: "A", lati: [{ misura: 200, quantita: 4 }, { misura: 160, quantita: 4 }] },
    { id_infisso: "B", lati: [{ misura: 200, quantita: 4 }, { misura: 160, quantita: 4 }] },
  ],
};

// ID in stile foglio di calcolo: 0 -> A, 25 -> Z, 26 -> AA, 27 -> AB, ...
function letterId(i) {
  let s = "";
  for (i += 1; i > 0; i = Math.floor((i - 1) / 26)) s = String.fromCharCode(65 + ((i - 1) % 26)) + s;
  return s;
}

// primo ID libero (A, B, ..., Z, AA, ...) non gia' usato da un altro infisso
function nextId() {
  const used = new Set([...tbody.querySelectorAll(".id")].map((x) => x.value.trim().toUpperCase()));
  for (let i = 0; ; i++) if (!used.has(letterId(i))) return letterId(i);
}

function addRow(inf) {
  const tr = document.createElement("tr");
  const l = (inf && inf.lati) || [{}, {}];
  if (!inf) inf = { id_infisso: nextId() };   // nuovo infisso: ID compilato in automatico
  const v = (x) => (x === undefined || x === null ? "" : esc(x));
  tr.innerHTML = `
    <td class="n muted"></td>
    <td><input class="id" value="${v(inf && inf.id_infisso)}"></td>
    <td><input class="m1" type="number" step="0.1" min="0.1" value="${v(l[0] && l[0].misura)}"></td>
    <td><input class="q1" type="number" step="1" min="1" value="${v(l[0] && l[0].quantita)}"></td>
    <td><input class="m2" type="number" step="0.1" min="0.1" value="${v(l[1] && l[1].misura)}"></td>
    <td><input class="q2" type="number" step="1" min="1" value="${v(l[1] && l[1].quantita)}"></td>
    <td><button type="button" class="del" title="Rimuovi">✕</button></td>`;
  tr.querySelector(".del").onclick = () => { tr.remove(); renumber(); };
  tbody.appendChild(tr);
  renumber();
}

function renumber() {
  const rows = [...tbody.rows];
  rows.forEach((tr, i) => {
    tr.querySelector(".n").textContent = i + 1;
    const id = tr.querySelector(".id");
    id.placeholder = letterId(i);
  });
  const max = Number($("count").dataset.max);
  $("count").textContent = `(${rows.length} / ${max})`;
  $("btn-add").disabled = rows.length >= max;
}

const num = (s) => (s.trim() === "" ? null : Number(s.replace(",", ".")));

function collect() {
  return {
    livello: $("level").value,
    parametri: {
      lunghezza_pannello: num($("p_len").value),
      kerf: num($("p_kerf").value),
      posti_appoggio: num($("p_slots").value),
      scarto_extra_max_per_cambio: num($("p_weight").value),
      margini: num($("p_marg").value),
      zone_non_tagliabili: num($("p_zone").value),
      ordine_misure: $("p_order").value,
    },
    infissi: [...tbody.rows].map((tr) => {
      const g = (c) => tr.querySelector(c).value;
      return {
        id_infisso: g(".id").trim() || tr.querySelector(".id").placeholder,
        lati: [{ misura: num(g(".m1")), quantita: num(g(".q1")) }, { misura: num(g(".m2")), quantita: num(g(".q2")) }],
      };
    }),
  };
}

function load(data) {
  const p = data.parametri || {};
  const set = (id, val) => { if (val !== undefined && val !== null) $(id).value = val; };
  set("p_len", p.lunghezza_pannello); set("p_kerf", p.kerf); set("p_slots", p.posti_appoggio);
  set("p_weight", p.scarto_extra_max_per_cambio); set("p_marg", p.margini); set("p_zone", p.zone_non_tagliabili); set("p_order", p.ordine_misure);
  tbody.innerHTML = "";
  (data.infissi || []).forEach(addRow);
}

function showErrors(list, title = "Input non valido") {
  const box = $("errors");
  if (!list || !list.length) { box.hidden = true; return; }
  box.innerHTML = `<b>${esc(title)}</b><ul>` + list.map((e) => `<li>${esc(e)}</li>`).join("") + "</ul>";
  box.hidden = false;
}

async function run() {
  showErrors(null);
  $("results").hidden = true;
  $("btn-run").disabled = true;
  try {
    const res = await fetch("/api/calcola", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(collect()) });
    const body = await res.json();
    if (!res.ok) { showErrors(body.errori); return; }
    $("progress").hidden = false;
    await poll(body.job);
  } catch (e) {
    showErrors([`Errore di comunicazione con il server: ${e}`]);
  } finally {
    $("btn-run").disabled = false;
  }
}

async function poll(job) {
  const t0 = Date.now();
  for (;;) {
    const j = await (await fetch(`/api/job/${job}`)).json();
    const pct = Math.round((j.progresso || 0) * 100);
    $("bar-fill").style.width = pct + "%";
    const sec = Math.round((Date.now() - t0) / 1000);
    $("progress-text").textContent = j.stato === "in_coda" ? "In coda (un altro calcolo è in corso)…" : `Calcolo in corso… ${pct}% · ${sec} s`;
    if (j.stato === "completato") { $("progress").hidden = true; render(j.risultato, job); return; }
    if (j.stato === "errore") { $("progress").hidden = true; showErrors(j.errori, j.titolo || "Errore"); return; }
    await new Promise((r) => setTimeout(r, 500));
  }
}

// Il piano (sezioni A-F) e' generato dal server a partire dalla soluzione validata.
function render(res, job) {
  $("results").innerHTML = `
    <section class="card downloads no-print">
      <b>Scarica:</b>
      <a href="/api/job/${job}/html?apri=1" target="_blank" rel="noopener">Apri piano da stampare</a>
      <a href="/api/job/${job}/html">Piano HTML</a>
      <a href="/api/job/${job}/json">Soluzione JSON</a>
      <a href="/api/job/${job}/txt">Piano in testo</a>
    </section>
    ${res.report_html}`;
  $("results").hidden = false;
  $("results").scrollIntoView({ behavior: "smooth", block: "start" });
}

$("btn-add").onclick = () => addRow();
$("btn-run").onclick = run;
$("btn-export").onclick = () => {
  const data = collect(); delete data.livello;
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }));
  a.download = "infissi.json"; a.click(); URL.revokeObjectURL(a.href);
};
$("btn-import").onclick = () => $("file").click();
$("file").onchange = async (ev) => {
  const f = ev.target.files[0]; if (!f) return;
  try { load(JSON.parse(await f.text())); showErrors(null); }
  catch (e) { showErrors([`File JSON non valido: ${e.message}`]); }
  ev.target.value = "";
};

load(ESEMPIO);
