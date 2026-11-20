"""
`ltspice_llm.monitor` sayfaları: koşumlar (``/``) ve şemalar (``/sema``). Dış kaynak yüklenmez (çevrimdışı çalışır).
Sayfaya gelen veri yalnızca ``textContent`` ile yazılır; tek istisna şema SVG'sidir: sunucunun `asc_view` ile ürettiği,
bütün metni kaçışlanmış SVG ``DOMParser`` ile ayrıştırılıp eklenir (CSP satır içi betiği ve olay özniteliğini de engeller).
"""

BASE_CSS = r"""
:root {
  --bg: #f6f6f3; --panel: #ffffff; --text: #1d1d1b; --muted: #6b6a64; --border: #e2e1da; --hover: #f3f2ee;
  --ok: #1a7f37; --fail: #c62828; --warn: #a15c00; --info: #1f5fbf;
  --sheet: #ffffff; --wire: #1f5fbf; --sym: #8a2b20; --cmt: #1f5fbf; --dir: #1d1d1b;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #141414; --panel: #1d1d1d; --text: #e8e6e1; --muted: #9b9890; --border: #2e2e2e; --hover: #252525;
    --ok: #57c771; --fail: #ff7070; --warn: #f0b04a; --info: #80aaff;
    --sheet: #171717; --wire: #80aaff; --sym: #ff9f8a; --cmt: #80aaff; --dir: #e8e6e1;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
header { position: sticky; top: 0; z-index: 1; display: flex; align-items: center; gap: 10px; padding: 10px 16px;
  background: var(--panel); border-bottom: 1px solid var(--border); }
h1 { margin: 0; font-size: 16px; font-weight: 600; }
nav { display: flex; gap: 4px; margin-left: 8px; }
nav a { padding: 3px 10px; border-radius: 6px; color: var(--muted); text-decoration: none; }
nav a.on { background: var(--hover); color: var(--text); font-weight: 600; }
.dot { flex: none; width: 9px; height: 9px; border-radius: 50%; background: var(--muted); }
.dot.on { background: var(--ok); } .dot.off { background: var(--fail); }
#meta { margin-left: auto; color: var(--muted); font-size: 12px; text-align: right; overflow-wrap: anywhere; }
.muted { color: var(--muted); } .ok { color: var(--ok); } .fail { color: var(--fail); } .warn { color: var(--warn); }
.info { color: var(--info); }
.empty { padding: 12px; color: var(--muted); }
button.link, a.link { padding: 1px 8px; border: 1px solid var(--border); border-radius: 6px; background: none;
  color: var(--info); font: inherit; font-size: 12px; cursor: pointer; text-decoration: none; }
code { font: 12px ui-monospace, Consolas, monospace; overflow-wrap: anywhere; }
"""

_HEADER = r"""<header><span id="dot" class="dot"></span><h1>LTspice monitörü</h1>
<nav><a href="/" class="{runs}">Koşumlar</a><a href="/sema" class="{schema}">Şemalar</a></nav>
<div id="meta">bağlanıyor…</div></header>"""

PAGE_HTML = r"""<!doctype html>
<html lang="tr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LTspice monitörü</title>
<style>/*BASE*/
main { display: grid; gap: 16px; max-width: 1240px; margin: 0 auto; padding: 16px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; }
.tile, section, .hint { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; }
.tile { padding: 10px 12px; }
.tile .k { color: var(--muted); font-size: 12px; }
.tile .v { font-size: 22px; font-variant-numeric: tabular-nums; }
.bar { height: 6px; margin-top: 6px; overflow: hidden; border-radius: 3px; background: var(--border); }
.bar > i { display: block; width: 0; height: 100%; background: var(--info); }
.hint { padding: 12px; line-height: 1.6; }
section { overflow: hidden; }
section h2 { margin: 0; padding: 10px 12px; font-size: 13px; font-weight: 600; border-bottom: 1px solid var(--border); }
.scroll { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { padding: 6px 10px; text-align: left; white-space: nowrap; border-bottom: 1px solid var(--border); }
th { color: var(--muted); font-weight: 500; font-size: 12px; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
td.wrap { min-width: 220px; white-space: normal; overflow-wrap: anywhere; }
tbody tr:hover, tr.sel { background: var(--hover); }
td.actions > * + * { margin-left: 4px; }
.pill { display: inline-block; padding: 0 8px; border: 1px solid currentColor; border-radius: 10px; font-size: 12px; }
pre { max-height: 440px; margin: 0; padding: 12px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere;
  font: 12px/1.4 ui-monospace, Consolas, monospace; }
</style>
</head>
<body>
/*HEADER*/
<main>
  <div class="tiles">
    <div class="tile"><div class="k">Çalışan koşum</div><div class="v" id="t-active">–</div></div>
    <div class="tile"><div class="k">Çekirdek bütçesi (kiralanan / toplam)</div><div class="v" id="t-budget">–</div>
      <div class="bar"><i id="t-budget-bar"></i></div></div>
    <div class="tile"><div class="k">Başarılı</div><div class="v ok" id="t-ok">–</div></div>
    <div class="tile"><div class="k">Başarısız / yarım kalan</div><div class="v fail" id="t-fail">–</div></div>
  </div>
  <div id="hint" class="hint" hidden><strong>Henüz olay yok.</strong> Koşumları yapan süreçte
    <code>LTSPICE_LLM_EVENTS=1</code> olmalı (PowerShell: <code>$env:LTSPICE_LLM_EVENTS='1'</code>, Bash:
    <code>export LTSPICE_LLM_EVENTS=1</code>). Olay dizini: <code id="hint-dir"></code></div>
  <section><h2>Çalışan koşumlar</h2><div class="scroll"><table>
    <thead><tr><th>Devre</th><th>Süreç</th><th>Aşama</th><th class="num">Süre / sınır</th>
      <th class="num">İş parçacığı</th><th class="num">.raw</th><th class="num">Son değişim</th>
      <th class="num">LTspice PID</th></tr></thead>
    <tbody id="active"></tbody></table></div>
    <div id="active-empty" class="empty">Çalışan koşum yok.</div></section>
  <section><h2>Biten koşumlar</h2><div class="scroll"><table>
    <thead><tr><th>Bitiş</th><th>Devre</th><th>Süreç</th><th>Sonuç</th><th class="num">Süre</th>
      <th class="num">İş parçacığı</th><th>Logdan: iş parçacığı · method</th><th class="num">Ölçüm</th>
      <th>Hata / uyarı</th><th></th></tr></thead>
    <tbody id="finished"></tbody></table></div>
    <div id="finished-empty" class="empty">Henüz biten koşum yok.</div></section>
  <section id="log-section" hidden><h2 id="log-title">Log</h2><pre id="log"></pre></section>
</main>
<script src="/app.js"></script>
</body>
</html>
""".replace("/*BASE*/", BASE_CSS).replace("/*HEADER*/", _HEADER.format(runs="on", schema=""))

PAGE_JS = r""""use strict";
const STAGES = {
  preparing: ["hazırlanıyor", "muted"],
  waiting_cpu: ["çekirdek bekliyor", "warn"],
  running: ["LTspice çalışıyor", "info"],
  stalled: ["takıldı, yeniden deneniyor", "warn"],
  collecting: ["sonuç okunuyor", "info"],
};
const STATUS = { ok: ["başarılı", "ok"], failed: ["başarısız", "fail"], abandoned: ["yarım kaldı", "warn"] };
const OUTCOMES = { exited: "bitti", timeout: "zaman aşımı", stalled: "takıldı" };
let selected = null;

const $ = (id) => document.getElementById(id);

function el(tag, text, cls) {
  const node = document.createElement(tag);
  if (text != null) node.textContent = String(text);
  if (cls) node.className = cls;
  return node;
}

function cell(row, text, cls) {
  const td = el("td", text, cls);
  row.appendChild(td);
  return td;
}

function pill(text, cls) {
  return el("span", text, "pill " + cls);
}

function schemaLink(source) {
  if (!source || !source.toLowerCase().endsWith(".asc")) return null;
  const a = el("a", "şema", "link");
  try {
    a.href = "/sema#path=" + encodeURIComponent(source);
  } catch (err) {  // eşleşmemiş vekil: URIError (ECMA-262 "Encode"); satır bağlantısız çizilir
    return null;
  }
  return a;
}

function num(x, digits) {
  return x.toLocaleString("tr-TR", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

function duration(s) {
  if (s == null) return "–";
  if (s < 10) return num(s, 2) + " sn";
  if (s < 60) return num(s, 1) + " sn";
  const m = Math.floor(s / 60);
  return m + " dk " + String(Math.floor(s - 60 * m)).padStart(2, "0") + " sn";
}

function size(b) {
  if (b == null) return "–";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (b >= 1024 && i < units.length - 1) { b /= 1024; i++; }
  return (i ? num(b, 1) : String(b)) + " " + units[i];
}

function clock(t) {
  return new Date(t * 1000).toLocaleTimeString("tr-TR");
}

function renderActive(rows) {
  const body = $("active");
  body.replaceChildren();
  for (const r of rows) {
    const tr = document.createElement("tr");
    const name = cell(tr, r.circuit + " ");
    const link = schemaLink(r.source);
    if (link) name.appendChild(link);
    cell(tr, r.process + " · " + r.pid, "muted");
    const stage = STAGES[r.stage] || [r.stage, "muted"];
    const label = stage[0] + (r.attempts > 1 ? " (" + r.attempts + ". deneme)" : "");
    const p = pill(label, stage[1]);
    if (r.waited != null && r.waited >= 1) p.title = "Çekirdek için " + duration(r.waited) + " beklendi";
    cell(tr, null).appendChild(p);
    cell(tr, duration(r.elapsed) + (r.timeout ? " / " + duration(r.timeout) : ""), "num");
    cell(tr, r.threads != null ? String(r.threads) : (r.guard ? "–" : "koruma kapalı"), "num");
    cell(tr, size(r.raw_bytes), "num");
    const since = cell(tr, duration(r.since_change), "num");
    if (r.stall && r.since_change != null && r.since_change > r.stall / 2) since.className += " warn";
    cell(tr, r.ltspice_pid != null ? String(r.ltspice_pid) : "–", "num");
    body.appendChild(tr);
  }
  $("active-empty").hidden = rows.length > 0;
}

let finishedShown = "";

function renderFinished(rows) {
  // yalnız değişince çizilir: her saniye çizilen "log"/"şema" düğmesi tıklamanın ortasında değişebilirdi
  const signature = JSON.stringify([selected, rows]);
  if (signature === finishedShown) return;
  finishedShown = signature;
  const body = $("finished");
  body.replaceChildren();
  for (const r of rows) {
    const tr = document.createElement("tr");
    if (r.run === selected) tr.className = "sel";
    cell(tr, clock(r.ended), "muted");
    cell(tr, r.circuit);
    cell(tr, r.process, "muted");
    const status = STATUS[r.status] || [r.status, "muted"];
    const result = cell(tr, null);
    result.appendChild(pill(status[0], status[1]));
    if (r.outcomes.some((o) => o !== "exited")) {
      result.appendChild(el("span", " " + r.outcomes.map((o) => OUTCOMES[o] || o).join(" → "), "muted"));
    }
    cell(tr, duration(r.elapsed), "num");
    cell(tr, r.threads != null ? String(r.threads) : "–", "num");
    const f = r.log_facts || {};
    const facts = f.stale ? "log değişti" : (f.threads != null ? f.threads : "–") + " · " + (f.method || "–");
    const mismatch = f.threads != null && r.threads != null && f.threads !== r.threads;
    cell(tr, facts, mismatch ? "warn" : (f.stale ? "muted" : ""));
    cell(tr, r.n_meas != null ? String(r.n_meas) : "–", "num");
    const counts = [r.n_errors ? r.n_errors + " hata" : "", r.n_warnings ? r.n_warnings + " uyarı" : ""]
      .filter(Boolean).join(", ");
    const message = r.exception || (r.errors && r.errors[0]) || (r.warnings && r.warnings[0]) || "";
    cell(tr, counts + (counts && message ? ": " : "") + message, "wrap " + (r.status === "ok" ? "muted" : "fail"));
    const actions = cell(tr, null, "actions");
    if (r.log) {
      const button = el("button", "log", "link");
      button.addEventListener("click", () => showLog(r));
      actions.appendChild(button);
    }
    const link = schemaLink(r.source);
    if (link) actions.appendChild(link);
    body.appendChild(tr);
  }
  $("finished-empty").hidden = rows.length > 0;
}

async function showLog(r) {
  selected = r.run;
  $("log-section").hidden = false;
  $("log-title").textContent = "Log: " + r.circuit + " (" + clock(r.ended) + ")";
  $("log").textContent = "yükleniyor…";
  try {
    const response = await fetch("/api/log?run=" + encodeURIComponent(r.run), { cache: "no-store" });
    const data = await response.json();
    $("log").textContent = data.text != null ? data.text : (data.error || "Log okunamadı.");
    if (data.path) $("log-title").textContent += " · " + data.path;
  } catch (err) {
    $("log").textContent = "Log okunamadı.";
  }
  $("log-section").scrollIntoView({ block: "nearest" });
}

function render(s) {
  $("meta").textContent = "Olay dizini: " + s.event_dir + " · güncellendi " + clock(s.now);
  $("t-active").textContent = String(s.active.length);
  const b = s.budget;
  $("t-budget").textContent = b.size != null ? b.in_use + " / " + b.size : "–";
  $("t-budget-bar").style.width = b.size ? Math.min(100, (100 * b.in_use) / b.size) + "%" : "0";
  $("t-ok").textContent = String(s.counts.ok);
  $("t-fail").textContent = String(s.counts.failed + s.counts.abandoned);
  $("hint").hidden = s.events_seen > 0;
  $("hint-dir").textContent = s.event_dir;
  renderActive(s.active);
  renderFinished(s.finished);
}

async function refresh() {
  let state;
  try {
    const response = await fetch("/api/state", { cache: "no-store" });
    if (!response.ok) throw new Error(String(response.status));
    state = await response.json();
  } catch (err) {
    $("dot").className = "dot off";
    $("meta").textContent = "Sunucuya ulaşılamıyor; monitör kapalı olabilir.";
    return;
  }
  $("dot").className = "dot on";
  render(state);
}

async function loop() {
  try {
    await refresh();
  } finally {  // bir satır çizilemese de yoklama sürer
    setTimeout(loop, 1000);
  }
}

loop();
"""

SCHEMA_HTML = r"""<!doctype html>
<html lang="tr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LTspice şemaları</title>
<style>/*BASE*/
main.viewer { display: grid; grid-template-columns: 280px minmax(0, 1fr); gap: 12px; height: calc(100vh - 49px);
  padding: 12px; }
aside, .canvas { min-height: 0; background: var(--panel); border: 1px solid var(--border); border-radius: 8px; }
aside { display: flex; flex-direction: column; overflow: hidden; }
aside .opts { padding: 10px 12px; border-bottom: 1px solid var(--border); font-size: 13px; }
aside .opts label { display: flex; gap: 6px; align-items: center; cursor: pointer; }
.add { display: flex; gap: 6px; margin-top: 8px; }
.add input { flex: 1; min-width: 0; padding: 4px 6px; border: 1px solid var(--border); border-radius: 6px;
  background: var(--bg); color: var(--text); font: inherit; font-size: 12px; }
.small { margin-top: 4px; font-size: 12px; overflow-wrap: anywhere; }
#roots { margin: 6px 0 0; padding: 0; list-style: none; font-size: 12px; }
#roots li { display: flex; gap: 4px; align-items: flex-start; }
.root-path { flex: 1; color: var(--muted); overflow-wrap: anywhere; }
.root-x { padding: 0 4px; border: 0; background: none; color: var(--muted); font: inherit; font-size: 15px;
  line-height: 1.1; cursor: pointer; }
.root-x:hover { color: var(--fail); }
#files { flex: 1; margin: 0; padding: 4px; overflow-y: auto; list-style: none; }
#files button { display: block; width: 100%; padding: 6px 8px; border: 0; border-radius: 6px; background: none;
  color: inherit; font: inherit; text-align: left; cursor: pointer; }
#files button:hover, #files li.sel button { background: var(--hover); }
#files li.sel .fname { font-weight: 600; }
.fname { display: block; overflow-wrap: anywhere; }
.fmeta { display: block; color: var(--muted); font-size: 12px; overflow-wrap: anywhere; }
.canvas { display: flex; flex-direction: column; overflow: hidden; }
.bar { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 10px; padding: 8px 12px;
  border-bottom: 1px solid var(--border); }
.bar .spacer { flex: 1; }
#name { font-weight: 600; overflow-wrap: anywhere; }
#info { font-size: 12px; overflow-wrap: anywhere; }
#warnings { padding: 6px 12px; font-size: 12px; border-bottom: 1px solid var(--border); overflow-wrap: anywhere; }
#stage { position: relative; flex: 1; min-height: 0; background: var(--sheet); touch-action: none; cursor: grab; }
#stage.drag { cursor: grabbing; }
#stage svg { display: block; width: 100%; height: 100%; }
.asc line, .asc rect, .asc ellipse, .asc path { fill: none; stroke-width: 1.5px; vector-effect: non-scaling-stroke;
  stroke-linecap: round; }
.asc .wire, .asc .ground, .asc .io { stroke: var(--wire); }
.asc .sym { stroke: var(--sym); }
.asc .part:hover .sym { stroke-width: 2.5px; }
.asc .note { stroke: var(--muted); }
.asc .missing rect { stroke: var(--warn); stroke-dasharray: 6 4; }
.asc .junction { fill: var(--wire); }
.asc text { font-family: system-ui, "Segoe UI", sans-serif; fill: var(--text); white-space: pre; }
.asc .t-dir { fill: var(--dir); font-weight: 600; }
.asc .t-cmt, .asc .t-flag { fill: var(--cmt); }
.asc .t-pin, .asc .t-sym, .asc .t-data { fill: var(--muted); }
.asc .t-missing { fill: var(--warn); }
@media (max-width: 760px) {
  main.viewer { grid-template-columns: minmax(0, 1fr); grid-template-rows: auto minmax(0, 1fr); height: auto;
    padding: 8px; }
  aside { max-height: 32vh; }
  .canvas { height: 70vh; }
}
</style>
</head>
<body>
/*HEADER*/
<main class="viewer">
  <aside>
    <div class="opts"><label><input type="checkbox" id="follow" checked> En son kaydedilen şemayı göster</label>
      <div class="add"><input id="add-path" type="text" autocomplete="off" spellcheck="false"
        placeholder="Klasör yolu yapıştırın" title="İzlenecek klasörün ya da .asc dosyasının yolu (Explorer'ın adres
        çubuğundan kopyalayın); Enter ya da Ekle"><button id="add-btn" class="link" type="button">Ekle</button></div>
      <div id="root-message" class="warn small" hidden></div>
      <ul id="roots"></ul></div>
    <ul id="files"></ul>
    <div id="files-empty" class="empty" hidden>İzlenen klasörlerde .asc yok. Yukarıya bir klasör yolu yapıştırıp
      "Ekle"ye basın.</div>
  </aside>
  <section class="canvas">
    <div class="bar"><span id="name">Şema seçilmedi</span><span id="info" class="muted"></span>
      <span class="spacer"></span><span class="muted" style="font-size:12px">tekerlek: yakınlaştır · sürükle: kaydır</span>
      <button id="fit" class="link" type="button">Sığdır</button></div>
    <div id="warnings" class="warn" hidden></div>
    <div id="stage"><div id="stage-empty" class="empty">Şema yükleniyor…</div></div>
  </section>
</main>
<script src="/sema.js"></script>
</body>
</html>
""".replace("/*BASE*/", BASE_CSS).replace("/*HEADER*/", _HEADER.format(runs="", schema="on"))

SCHEMA_JS = r""""use strict";
const $ = (id) => document.getElementById(id);
let files = [];
let current = null;
let currentStamp = null;
let base = null;
let view = null;
let svg = null;
let follow = true;
let drag = null;
let busy = false;
let requests = 0;
// Listeler yalnız içerikleri değişince yeniden çizilir: her saniye çizilen düğme tıklamanın ortasında değişebilirdi
let rootsShown = "";
let listShown = "";

function el(tag, text, cls) {
  const node = document.createElement(tag);
  if (text != null) node.textContent = String(text);
  if (cls) node.className = cls;
  return node;
}

function clock(t) {
  return new Date(t * 1000).toLocaleTimeString("tr-TR");
}

function stamp(t) {
  return new Date(t * 1000).toLocaleString("tr-TR");
}

function showMessage(text) {
  svg = null;
  $("stage").replaceChildren(el("div", text, "empty"));
}

function applyView() {
  if (svg && view) svg.setAttribute("viewBox", view.join(" "));
}

function fit() {
  if (base) {
    view = base.slice();
    applyView();
  }
}

function renderRoots(roots) {
  const signature = JSON.stringify(roots);
  if (signature === rootsShown) return;
  rootsShown = signature;
  const list = $("roots");
  list.replaceChildren();
  for (const root of roots) {
    const li = el("li", null, null);
    li.appendChild(el("span", root, "root-path"));
    const remove = el("button", "×", "root-x");
    remove.type = "button";
    remove.title = "Bu klasörü izlemeyi bırak";
    remove.addEventListener("click", () => changeRoots("remove", root));
    li.appendChild(remove);
    list.appendChild(li);
  }
}

async function changeRoots(action, path) {
  const message = $("root-message");
  let response, data;
  try {
    response = await fetch("/api/roots/" + action, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-LTspice-Monitor": "1" },
      body: JSON.stringify({ path: path }),
    });
    data = await response.json();
  } catch (err) {
    message.hidden = false;
    message.textContent = "Sunucuya ulaşılamadı.";
    return false;
  }
  const text = response.ok ? (data.warning || "") : (data.error || "İşlem yapılamadı.");
  message.hidden = !text;
  message.textContent = text;
  if (data.roots) renderRoots(data.roots);
  poll();
  return response.ok;
}

async function addRoot() {
  const input = $("add-path");
  const path = input.value.trim();
  if (path && await changeRoots("add", path)) input.value = "";
}

function renderList() {
  const signature = JSON.stringify([current, files]);  // klasör etiketi kök eklenince/bırakılınca da değişir
  if (signature === listShown) return;
  listShown = signature;
  const list = $("files");
  list.replaceChildren();
  for (const f of files) {
    const li = el("li", null, f.path === current ? "sel" : "");
    const button = el("button", null, null);
    button.type = "button";
    button.title = f.path;
    button.appendChild(el("span", f.name, "fname"));
    button.appendChild(el("span", f.folder + " · " + stamp(f.mtime), "fmeta"));
    button.addEventListener("click", () => {
      follow = false;
      $("follow").checked = false;
      load(f.path);
    });
    li.appendChild(button);
    list.appendChild(li);
  }
  $("files-empty").hidden = files.length > 0;
}

async function load(path) {
  // Hedef beklemeden önce yazılır: yoklama bu sırada başka bir şemayı istemesin. Yalnız son isteğin yanıtı çizilir.
  const request = ++requests;
  const changed = path !== current;
  current = path;
  let response, data;
  try {
    response = await fetch("/api/schematic?path=" + encodeURIComponent(path), { cache: "no-store" });
    data = await response.json();
  } catch (err) {
    if (request === requests) showMessage("Şema alınamadı.");
    return;
  }
  if (request !== requests) return;
  if (response.ok) current = data.path;  // sunucunun yazımı listedekiyle aynı (kısa 8.3 adlar açılmış)
  try {
    history.replaceState(null, "", "#path=" + encodeURIComponent(current));
  } catch (err) {  // çözülen yol eşleşmemiş vekil taşıyorsa (URIError) adres güncellenmez
  }
  if (!response.ok) {
    currentStamp = null;
    $("name").textContent = path;
    $("info").textContent = "";
    $("warnings").hidden = true;
    showMessage(data.error || "Şema açılamadı.");
    renderList();
    return;
  }
  const doc = new DOMParser().parseFromString(data.svg, "image/svg+xml");
  if (doc.documentElement.nodeName !== "svg") {
    showMessage("Şema SVG'si çözülemedi.");
    return;
  }
  currentStamp = data.stamp;
  base = data.viewbox;
  if (changed || !view) view = base.slice();
  svg = document.importNode(doc.documentElement, true);
  $("stage").replaceChildren(svg);
  applyView();
  $("name").textContent = data.name;
  const c = data.counts;
  $("info").textContent = data.folder + " · kaydedildi " + stamp(data.mtime) + " · " + c.symbols + " sembol, "
    + c.wires + " kablo, " + c.flags + " etiket";
  $("warnings").hidden = data.warnings.length === 0;
  $("warnings").textContent = data.warnings.join(" ");
  renderList();
}

async function poll() {
  if (busy) return;
  busy = true;
  try {
    const response = await fetch("/api/schematics", { cache: "no-store" });
    if (!response.ok) throw new Error(String(response.status));
    const data = await response.json();
    $("dot").className = "dot on";
    $("meta").textContent = (data.truncated ? "liste kısaltıldı · " : "") + "güncellendi " + clock(data.now);
    files = data.files;
    renderRoots(data.roots);
    renderList();
    let target = follow && files.length ? files[0].path : current;
    if (!target && files.length) target = files[0].path;
    const entry = files.find((f) => f.path === target);
    if (target && (target !== current || (entry && entry.stamp !== currentStamp))) await load(target);
    else if (!target) showMessage("Gösterilecek şema yok.");
  } catch (err) {
    $("dot").className = "dot off";
    $("meta").textContent = "Sunucuya ulaşılamıyor; monitör kapalı olabilir.";
  } finally {
    busy = false;
  }
}

const stage = $("stage");
stage.addEventListener("wheel", (e) => {
  if (!svg || !view) return;
  e.preventDefault();
  const ctm = svg.getScreenCTM();
  if (!ctm) return;
  const p = new DOMPoint(e.clientX, e.clientY).matrixTransform(ctm.inverse());
  const k = Math.exp(Math.max(-1, Math.min(1, e.deltaY * 0.0015)));
  view = [p.x - (p.x - view[0]) * k, p.y - (p.y - view[1]) * k, view[2] * k, view[3] * k];
  applyView();
}, { passive: false });
stage.addEventListener("pointerdown", (e) => {
  if (!svg || !view || e.button !== 0) return;
  drag = { x: e.clientX, y: e.clientY, view: view.slice(), scale: svg.getScreenCTM().a };
  stage.setPointerCapture(e.pointerId);
  stage.classList.add("drag");
});
stage.addEventListener("pointermove", (e) => {
  if (!drag) return;
  view = [drag.view[0] - (e.clientX - drag.x) / drag.scale, drag.view[1] - (e.clientY - drag.y) / drag.scale,
    drag.view[2], drag.view[3]];
  applyView();
});
function endDrag() {
  drag = null;
  stage.classList.remove("drag");
}
stage.addEventListener("pointerup", endDrag);
stage.addEventListener("pointercancel", endDrag);
stage.addEventListener("dblclick", fit);
$("fit").addEventListener("click", fit);
$("add-btn").addEventListener("click", addRoot);
$("add-path").addEventListener("keydown", (e) => {
  if (e.key === "Enter") addRoot();
});
$("follow").addEventListener("change", (e) => {
  follow = e.target.checked;
  poll();
});

const wanted = location.hash.match(/^#path=(.+)$/);
if (wanted) {
  follow = false;
  $("follow").checked = false;
  let target = null;
  try {
    target = decodeURIComponent(wanted[1]);
  } catch (err) {  // elle bozulmuş adres (ör. %E0%A4%A): yok sayılır, sayfa yoklamaya başlar
    target = null;
  }
  if (target) load(target);
}

async function loop() {
  await poll();
  setTimeout(loop, 1000);
}

loop();
"""
