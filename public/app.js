// Sealed Nectar study cards: page logic.
// Plain ES module, no build step: Vercel serves it straight from public/.
// PDF text is extracted here in the browser (pdf.js), so only text is sent to the API.

const $ = (id) => document.getElementById(id);

// ---------- storage (every call is guarded: private mode and full disks throw) ----------
const LS = {
  get(k, d) { try { const v = localStorage.getItem("sn:" + k); return v ? JSON.parse(v) : d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem("sn:" + k, JSON.stringify(v)); } catch { /* storage unavailable */ } },
  del(k) { try { localStorage.removeItem("sn:" + k); } catch { /* storage unavailable */ } },
};

const BookStore = {
  open() {
    return new Promise((res, rej) => {
      const r = indexedDB.open("sealed-nectar", 1);
      r.onupgradeneeded = () => r.result.createObjectStore("book");
      r.onsuccess = () => res(r.result);
      r.onerror = () => rej(r.error);
    });
  },
  async run(mode, fn) {
    const db = await this.open();
    return new Promise((res, rej) => {
      const tx = db.transaction("book", mode);
      const req = fn(tx.objectStore("book"));
      tx.oncomplete = () => res(req && req.result);
      tx.onerror = () => rej(tx.error);
    });
  },
  async get() { try { return (await this.run("readonly", (s) => s.get("current"))) || null; } catch { return null; } },
  async put(v) { try { await this.run("readwrite", (s) => s.put(v, "current")); return true; } catch { return false; } },
  async clear() { try { await this.run("readwrite", (s) => s.delete("current")); } catch { /* ignore */ } },
};

// ---------- state ----------
const state = {
  config: null,
  settings: { passcode: "", apiKey: "", model: "", mcqDeck: "", clozeDeck: "", ppd: 7, days: 85, ...LS.get("settings", {}) },
  plan: { day: 1, chapter: "", first: 1, last: 7, mode: "pdf", exportedOn: null, ...LS.get("plan", {}) },
  opts: { mcq: "auto", cloze: 15, scope: "strict", ...LS.get("opts", {}) },
  pasted: LS.get("pasted", ""),
  book: null,            // { name, size, pages }
  text: "",
  words: 0,
  results: LS.get("results", null),
  files: null,
  editing: null,         // { kind, index }
  tab: "mcq",
  busy: false,
};

const save = {
  settings: () => LS.set("settings", state.settings),
  plan: () => LS.set("plan", state.plan),
  opts: () => LS.set("opts", state.opts),
  results: () => (state.results ? LS.set("results", state.results) : LS.del("results")),
};

const today = () => new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 10);

// ---------- tiny DOM helper ----------
function el(tag, attrs = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === false || v == null) continue;
    if (k === "class") n.className = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (k === "text") n.textContent = v;
    else n.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) n.append(kid);
  return n;
}

let toastTimer;
function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), 3800);
}

function banner(kind, ...lines) {
  return el("div", { class: `banner ${kind}`, role: kind === "error" ? "alert" : null },
    ...lines.map((l) => el("p", { text: l })));
}

// ---------- API ----------
async function api(path, body, auth = false) {
  const headers = { "Content-Type": "application/json" };
  if (auth) {
    if (state.settings.passcode) headers["X-App-Passcode"] = state.settings.passcode;
    if (state.settings.apiKey) headers["X-OpenAI-Key"] = state.settings.apiKey;
  }
  let res;
  try {
    res = await fetch(path, { method: body ? "POST" : "GET", headers, body: body ? JSON.stringify(body) : undefined });
  } catch {
    throw Object.assign(new Error("Couldn't reach the server. Check your connection and try again."), { status: 0 });
  }
  let data = null;
  try { data = await res.json(); } catch { /* non-JSON error page */ }
  if (!res.ok) {
    let msg = `Request failed (${res.status}).`;
    if (res.status === 504) msg = "The server stopped after 5 minutes. Try fewer questions or a faster model in Settings.";
    else if (res.status === 413) msg = "That much text is too large to send. Choose fewer pages.";
    else if (data && typeof data.detail === "string") msg = data.detail;
    else if (data && Array.isArray(data.detail)) msg = data.detail.map((d) => d.msg).join("; ");
    throw Object.assign(new Error(msg), { status: res.status });
  }
  return data;
}

// ---------- PDF (pdf.js, loaded only when a book is used) ----------
let pdfjs = null;
let pdfDoc = null;

async function openPdf(data) {
  if (!pdfjs) {
    pdfjs = await import("/vendor/pdfjs/pdf.min.mjs");
    pdfjs.GlobalWorkerOptions.workerSrc = "/vendor/pdfjs/pdf.worker.min.mjs";
  }
  if (pdfDoc) { pdfDoc.destroy(); pdfDoc = null; }
  // pdf.js takes ownership of the buffer it is given, so pass a copy.
  pdfDoc = await pdfjs.getDocument({ data: new Uint8Array(data.slice(0)) }).promise;
  return pdfDoc.numPages;
}

async function pageText(n) {
  const page = await pdfDoc.getPage(n);
  const content = await page.getTextContent();
  let out = "";
  let prev = null;
  for (const it of content.items) {
    if (typeof it.str !== "string") continue;
    if (prev && !prev.hasEOL) {
      const lineHeight = Math.abs(prev.transform[3]) || 10;
      const sameLine = Math.abs(it.transform[5] - prev.transform[5]) < lineHeight * 0.5;
      if (!sameLine) out += "\n";
      else {
        const gap = it.transform[4] - (prev.transform[4] + prev.width);
        if (gap > lineHeight * 0.15 && !/\s$/.test(out) && !/^\s/.test(it.str)) out += " ";
      }
    }
    out += it.str;
    if (it.hasEOL) out += "\n";
    prev = it;
  }
  return out;
}

// ---------- reading selection ----------
const MAX_PAGES = 60;
let extractSeq = 0;
let extractTimer;

function clampPages() {
  const p = state.plan;
  const max = state.book ? state.book.pages : 9999;
  p.first = Math.min(Math.max(1, Math.round(p.first) || 1), max);
  p.last = Math.min(Math.max(p.first, Math.round(p.last) || p.first), max, p.first + MAX_PAGES - 1);
}

function scheduleExtract() {
  clearTimeout(extractTimer);
  extractTimer = setTimeout(extract, 250);
}

async function extract() {
  const seq = ++extractSeq;
  const p = state.plan;
  if (p.mode === "paste") {
    state.text = state.pasted;
    return afterText();
  }
  if (!pdfDoc) {
    state.text = "";
    return afterText();
  }
  clampPages();
  renderPages();
  setStatus(`Reading pages ${p.first} to ${p.last}…`);
  let text = "";
  try {
    for (let n = p.first; n <= p.last; n++) {
      text += (await pageText(n)) + "\n\n";
      if (seq !== extractSeq) return;               // a newer selection replaced this one
    }
  } catch {
    if (seq === extractSeq) setStatus("Those pages couldn't be read. Try another range.", true);
    return;
  }
  state.text = text;
  afterText();
}

function afterText() {
  state.words = (state.text.match(/\S+/g) || []).length;
  const p = state.plan;
  const prev = $("preview");
  if (p.mode === "pdf" && !state.book) setStatus("");
  else if (!state.words) setStatus(p.mode === "paste" ? "" : "No text on these pages.", p.mode === "pdf");
  else if (p.mode === "pdf" && state.words < 30 * (p.last - p.first + 1) / 2) {
    setStatus(`Only ${state.words} words found. The PDF may be scanned images; paste the text instead.`, true);
  } else {
    const where = p.mode === "pdf" ? ` from pages ${p.first} to ${p.last}` : "";
    setStatus(`${state.words.toLocaleString()} words${where}.`);
  }
  prev.hidden = !state.words;
  $("preview-text").textContent = state.text.slice(0, 8000) + (state.text.length > 8000 ? " …" : "");
  renderOptions();
  renderJourney();
  renderDock();
}

function setStatus(msg, isError = false) {
  const s = $("extract-status");
  s.textContent = msg;
  s.classList.toggle("error", isError);
}

function selectionSig() {
  const p = state.plan;
  return `${p.mode}|${p.mode === "pdf" ? `${state.book ? state.book.name : ""}|${p.first}|${p.last}` : ""}|${state.words}`;
}

// ---------- options ----------
function suggestCount(words) {
  for (const [limit, n] of state.config.suggest) if (limit === null || words <= limit) return n;
  return 20;
}
function mcqCount() {
  return state.opts.mcq === "auto" ? suggestCount(state.words || 2000) : Number(state.opts.mcq);
}

function segOptions(container, name, options, current) {
  container.replaceChildren(...options.map(([value, label]) =>
    el("label", {}, el("input", { type: "radio", name, value: String(value), checked: String(value) === String(current) }),
      el("span", { text: label }))));
}

function renderOptions() {
  if (!state.config) return;
  const auto = suggestCount(state.words || 2000);
  segOptions($("mcq-seg"), "mcq", [["auto", `Auto ${auto}`], ...state.config.mcq_counts.map((n) => [n, String(n)])], state.opts.mcq);
  segOptions($("cloze-seg"), "cloze", state.config.cloze_max.map((n) => [n, String(n)]), state.opts.cloze);
  const n = mcqCount();
  const s = state.config.splits[String(n)];
  $("mix").textContent = `${s.Easy} easy, ${s.Medium} medium and ${s.Hard} hard.`
    + (state.opts.mcq === "auto" ? " Auto picks the number from the length of the text." : "");
  for (const r of document.querySelectorAll('input[name="scope"]')) r.checked = r.value === state.opts.scope;
}

// ---------- header journey ----------
function renderJourney() {
  const { day } = state.plan;
  const total = Math.max(1, state.settings.days);
  const title = $("day-title");
  title.replaceChildren(`Day ${day} `, el("small", { text: `of ${total}` }));
  const frac = total > 1 ? Math.min(1, (day - 1) / (total - 1)) : 1;
  $("route-walked").style.strokeDashoffset = String(1000 * (1 - frac));
  const path = $("route-path");
  const pt = path.getPointAtLength(path.getTotalLength() * frac);
  $("route-here").setAttribute("cx", pt.x.toFixed(1));
  $("route-here").setAttribute("cy", pt.y.toFixed(1));
  const p = state.plan;
  $("pages-label").textContent = p.mode === "pdf" && state.book ? `Pages ${p.first} to ${p.last}` : "";
}

function renderPages() {
  $("first").value = state.plan.first;
  $("last").value = state.plan.last;
  const max = state.book ? state.book.pages : "";
  $("first").max = max; $("last").max = max;
}

function renderBook() {
  const row = $("book-row");
  const name = $("book-name");
  if (state.book) {
    row.classList.add("loaded");
    name.replaceChildren(state.book.name, el("small", { text: `${state.book.pages} pages, saved on this device` }));
    $("book-btn-label").textContent = "Change";
  } else {
    row.classList.remove("loaded");
    name.replaceChildren("No book chosen", el("small", { text: "Choose it once; it stays on this device." }));
    $("book-btn-label").textContent = "Choose PDF";
  }
  $("pages").hidden = !state.book;
}

function renderMode() {
  const pdf = state.plan.mode === "pdf";
  $("pdf-box").hidden = !pdf;
  $("paste-box").hidden = pdf;
  for (const r of document.querySelectorAll('input[name="mode"]')) r.checked = r.value === state.plan.mode;
}

// ---------- results ----------
function clozeNodes(text) {
  const out = [];
  const re = /\{\{c(\d+)::(.*?)(?:::(.*?))?\}\}/g;
  let last = 0;
  let m;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    out.push(el("mark", {}, m[2], el("sup", { text: m[1] })));
    last = re.lastIndex;
  }
  out.push(text.slice(last));
  return out;
}

function keepSwitch(item) {
  return el("label", { class: "switch" }, "Keep",
    el("input", { type: "checkbox", role: "switch", checked: item.keep, onchange: (e) => {
      item.keep = e.target.checked;
      e.target.closest(".card").classList.toggle("dropped", !item.keep);
      save.results(); renderCounts(); refreshFiles();
    } }));
}

function detailsBlock(summary, ...kids) {
  return el("details", {}, el("summary", { text: summary }), ...kids);
}

function mcqCard(item, index) {
  const editing = state.editing && state.editing.kind === "mcq" && state.editing.index === index;
  const li = el("li", { class: `card${item.verified ? "" : " flagged"}${item.keep ? "" : " dropped"}` });
  li.append(el("div", { class: "card-head" },
    el("span", { class: `tag ${item.difficulty.toLowerCase()}`, text: item.difficulty }),
    el("span", { class: "tag", text: item.focus }),
    el("span", { class: "spacer" }), keepSwitch(item)));
  if (editing) {
    li.append(mcqForm(item, index));
    return li;
  }
  li.append(el("p", { class: "q", text: item.question }));
  li.append(el("ol", { class: "opts" }, ...item.options.map((o, i) => {
    const letter = "ABCD"[i];
    return el("li", { class: letter === item.answer ? "correct" : null },
      el("b", { text: letter }), el("span", { text: o }),
      letter === item.answer ? el("span", { class: "sr-only", text: " (correct)" }) : null);
  })));
  if (!item.verified) li.append(el("p", { class: "flag-note", text: `Check this one: ${item.check_notes}.` }));
  li.append(detailsBlock("Context and source",
    item.context ? el("p", { text: item.context }) : null,
    item.source_excerpt ? el("blockquote", { text: item.source_excerpt }) : null));
  li.append(el("div", { class: "card-actions" },
    el("button", { type: "button", class: "link-btn", text: "Edit", onclick: () => startEdit("mcq", index) })));
  return li;
}

function mcqForm(item, index) {
  const f = el("form", { class: "edit-form", onsubmit: (e) => {
    e.preventDefault();
    const d = new FormData(f);
    item.question = d.get("question").trim();
    item.options = ["A", "B", "C", "D"].map((L) => d.get(L).trim());
    item.answer = d.get("answer");
    item.context = d.get("context").trim();
    finishEdit();
  } });
  const field = (label, node) => el("label", { class: "field" }, el("span", { text: label }), node);
  f.append(field("Question", el("textarea", { name: "question", required: true }, item.question)));
  ["A", "B", "C", "D"].forEach((L, i) => f.append(field(`Option ${L}`, el("input", { type: "text", name: L, value: item.options[i], required: true }))));
  f.append(field("Correct option", el("select", { name: "answer" },
    ...["A", "B", "C", "D"].map((L) => el("option", { value: L, selected: L === item.answer, text: L })))));
  f.append(field("Context", el("textarea", { name: "context" }, item.context)));
  f.append(el("div", { class: "row" },
    el("button", { type: "button", class: "btn small", text: "Cancel", onclick: () => { state.editing = null; renderResults(); } }),
    el("button", { type: "submit", class: "btn small primary", text: "Save" })));
  return f;
}

function clozeCard(item, index) {
  const editing = state.editing && state.editing.kind === "cloze" && state.editing.index === index;
  const li = el("li", { class: `card${item.verified ? "" : " flagged"}${item.keep ? "" : " dropped"}` });
  li.append(el("div", { class: "card-head" },
    el("span", { class: "tag", text: item.category }),
    el("span", { class: "spacer" }), keepSwitch(item)));
  if (editing) {
    li.append(clozeForm(item));
    return li;
  }
  li.append(el("p", { class: "cloze" }, ...clozeNodes(item.text)));
  if (!item.verified) li.append(el("p", { class: "flag-note", text: `Check this one: ${item.check_notes}.` }));
  li.append(detailsBlock("Background and source",
    item.extra ? el("p", { text: item.extra }) : null,
    item.source_excerpt ? el("blockquote", { text: item.source_excerpt }) : null));
  li.append(el("div", { class: "card-actions" },
    el("button", { type: "button", class: "link-btn", text: "Edit", onclick: () => startEdit("cloze", index) })));
  return li;
}

function clozeForm(item) {
  const err = el("p", { class: "flag-note", hidden: true });
  const f = el("form", { class: "edit-form", onsubmit: (e) => {
    e.preventDefault();
    const d = new FormData(f);
    const text = d.get("text").trim();
    if (!/\{\{c\d+::[^{}]+?\}\}/.test(text)) {
      err.textContent = "Keep at least one deletion written as {{c1::answer}}.";
      err.hidden = false;
      return;
    }
    item.text = text;
    item.extra = d.get("extra").trim();
    finishEdit();
  } });
  const field = (label, node) => el("label", { class: "field" }, el("span", { text: label }), node);
  f.append(field("Card text", el("textarea", { name: "text", required: true, spellcheck: "false" }, item.text)));
  f.append(err);
  f.append(field("Background", el("textarea", { name: "extra" }, item.extra)));
  f.append(el("div", { class: "row" },
    el("button", { type: "button", class: "btn small", text: "Cancel", onclick: () => { state.editing = null; renderResults(); } }),
    el("button", { type: "submit", class: "btn small primary", text: "Save" })));
  return f;
}

function startEdit(kind, index) { state.editing = { kind, index }; renderResults(); }
function finishEdit() { state.editing = null; save.results(); renderResults(); refreshFiles(); }

function pendingBlock(kind) {
  const r = state.results;
  const secs = Math.round((Date.now() - r.startedAt) / 1000);
  const what = kind === "mcq" ? `Writing ${r.mcq.n} questions` : "Finding Hijrah route facts";
  return el("div", { class: "pending" }, el("div", { class: "spinner", "aria-hidden": "true" }),
    el("span", { text: `${what}… ${secs} s. Keep this screen open.` }));
}

function renderTab(kind) {
  const box = $(`tab-${kind}`);
  const r = state.results[kind];
  const kids = [];
  if (!r) { box.replaceChildren(); return; }
  if (r.pending) kids.push(pendingBlock(kind));
  else if (r.error) kids.push(banner("error", r.error, "Tap Generate again to retry."));
  else {
    for (const w of r.warnings || []) kids.push(banner("warn", w));
    if (!r.items.length) {
      kids.push(el("p", { class: "empty", text: kind === "mcq"
        ? "No questions came back. Try again."
        : "No Hijrah route facts in these pages. That's normal for chapters before or after the migration." }));
    } else {
      kids.push(el("ol", { class: "cards" }, ...r.items.map((it, i) => (kind === "mcq" ? mcqCard(it, i) : clozeCard(it, i)))));
    }
  }
  box.replaceChildren(...kids);
}

function renderCounts() {
  for (const kind of ["mcq", "cloze"]) {
    const r = state.results && state.results[kind];
    const c = $(`${kind}-count`);
    if (!r || r.pending) { c.textContent = r ? "working" : ""; c.classList.remove("flagged"); continue; }
    if (r.error) { c.textContent = "failed"; c.classList.add("flagged"); continue; }
    const kept = r.items.filter((i) => i.keep).length;
    const flagged = r.items.filter((i) => i.keep && !i.verified).length;
    c.textContent = `${kept} kept` + (flagged ? `, ${flagged} to check` : "");
    c.classList.toggle("flagged", flagged > 0);
  }
}

function renderResults() {
  const has = !!state.results;
  $("results").hidden = !has;
  if (!has) return;
  for (const r of document.querySelectorAll('input[name="tab"]')) r.checked = r.value === state.tab;
  $("tab-mcq").hidden = state.tab !== "mcq";
  $("tab-cloze").hidden = state.tab !== "cloze";
  renderTab("mcq");
  renderTab("cloze");
  renderCounts();
  renderDock();
}

// ---------- generate ----------
async function generate() {
  if (state.busy || !state.config) return;
  const chapter = $("chapter").value.trim();
  const box = $("banners");
  box.replaceChildren();
  if (!chapter) {
    box.append(banner("error", "Add a chapter name first. It becomes the Anki tag."));
    $("chapter").focus();
    return;
  }
  if (state.words < 100) {
    box.append(banner("error", state.plan.mode === "pdf"
      ? "Choose the book PDF and a page range with at least 100 words."
      : "Paste at least 100 words of text."));
    return;
  }
  const cfg = state.config;
  if (!state.settings.apiKey && !cfg.server_key) return openSettings("Add your OpenAI key to generate cards.");
  if (!state.settings.apiKey && cfg.passcode_required && !state.settings.passcode) {
    return openSettings("Enter the passcode to generate cards.");
  }

  state.busy = true;
  state.editing = null;
  state.files = null;
  state.tab = "mcq";
  const n = mcqCount();
  state.results = {
    chapter, day: state.plan.day, sig: selectionSig(), startedAt: Date.now(),
    mcq: { pending: true, n }, cloze: { pending: true },
  };
  renderResults();
  $("results").scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });

  let lock = null;
  try { lock = await navigator.wakeLock?.request("screen"); } catch { /* not supported */ }
  const tick = setInterval(() => {
    for (const k of ["mcq", "cloze"]) if (state.results[k].pending) renderTab(k);
  }, 1000);

  const body = {
    text: state.text, chapter, n_mcq: n, max_cloze: Number(state.opts.cloze),
    scope: state.opts.scope, model: state.settings.model || null,
  };
  let authFailed = false;
  const run = (kind) => api(`/api/generate/${kind}`, body, true)
    .then((d) => { state.results[kind] = { items: d.items, warnings: d.warnings }; })
    .catch((e) => {
      state.results[kind] = { error: e.message };
      if (e.status === 401) authFailed = true;
    })
    .finally(() => { renderTab(kind); renderCounts(); });
  await Promise.all([run("mcq"), run("cloze")]);

  clearInterval(tick);
  try { await lock?.release(); } catch { /* already released */ }
  state.busy = false;
  save.results();
  renderResults();
  refreshFiles();
  if (authFailed) openSettings("That passcode didn't work. Check it and try again.");
}

// ---------- export ----------
let filesTimer;
function refreshFiles() {
  state.files = null;
  renderDock();
  clearTimeout(filesTimer);
  filesTimer = setTimeout(buildFiles, 250);
}

async function buildFiles() {
  const r = state.results;
  if (!r || state.busy) return;
  const mcq = r.mcq && r.mcq.items ? r.mcq.items : [];
  const cloze = r.cloze && r.cloze.items ? r.cloze.items : [];
  if (!mcq.length && !cloze.length) return;
  try {
    const d = await api("/api/export", {
      chapter: r.chapter, day: r.day || null,
      mcq_deck: state.settings.mcqDeck || state.config.decks.mcq,
      cloze_deck: state.settings.clozeDeck || state.config.decks.cloze,
      mcq, cloze,
    });
    state.files = d.files;
    if (d.dropped_cloze) toast(`${d.dropped_cloze} edited route card${d.dropped_cloze > 1 ? "s have" : " has"} no valid {{c1::...}} deletion and was left out.`);
  } catch (e) {
    $("banners").replaceChildren(banner("error", `Couldn't build the Anki files: ${e.message}`));
  }
  renderDock();
}

const isTouch = () => matchMedia("(pointer: coarse)").matches;

async function sendFile(kind) {
  const f = state.files && state.files[kind];
  if (!f || !f.count) return;
  const file = new File([f.csv], f.name, { type: "text/csv" });
  if (isTouch() && navigator.canShare && navigator.canShare({ files: [file] })) {
    try {
      await navigator.share({ files: [file] });
      markExported(`Shared ${f.name}. Choose Anki in the share sheet to import it.`);
      return;
    } catch (e) {
      if (e && e.name === "AbortError") return;   // closed the share sheet
    }
  }
  const url = URL.createObjectURL(file);
  const a = el("a", { href: url, download: f.name });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 4000);
  markExported(`Saved ${f.name}. Open it with Anki to import.`);
}

function markExported(msg) {
  state.plan.exportedOn = today();
  save.plan();
  toast(msg);
  const box = $("banners");
  box.replaceChildren(el("div", { class: "banner warn" },
    el("p", { text: "Tomorrow the app opens on the next day and the next pages." }),
    el("p", {}, el("button", { type: "button", class: "link-btn", text: "Set up the next reading now", onclick: () => advance(true) }))));
}

function renderDock() {
  const r = state.results;
  const done = r && !state.busy && ((r.mcq && r.mcq.items) || (r.cloze && r.cloze.items));
  const showExport = !!done && r.sig === selectionSig();
  $("dock-generate").hidden = showExport;
  $("dock-export").hidden = !showExport;
  const gen = $("generate");
  gen.disabled = state.busy || !state.config;
  gen.textContent = state.busy ? "Generating…" : (r && !showExport ? "Generate cards for these pages" : "Generate cards");
  $("regenerate").disabled = state.busy;
  const f = state.files;
  const both = $("send-both");
  both.disabled = !f || !f.both.count;
  both.textContent = !f ? "Preparing files…" : `Send to Anki (${f.both.count} cards)`;
  $("send-mcq").disabled = !f || !f.mcq.count;
  $("send-cloze").disabled = !f || !f.cloze.count;
  $("send-mcq").textContent = f ? `Questions file (${f.mcq.count})` : "Questions file";
  $("send-cloze").textContent = f ? `Route cards file (${f.cloze.count})` : "Route cards file";
}

// ---------- daily rhythm ----------
function advance(now = false) {
  const p = state.plan;
  const span = Math.max(1, Number(state.settings.ppd) || 7);
  p.day = Math.min(p.day + 1, Math.max(1, state.settings.days));
  if (p.mode === "pdf") {
    p.first = p.last + 1;
    p.last = p.first + span - 1;
    clampPages();
  } else {
    state.pasted = "";
    LS.set("pasted", "");
    $("paste").value = "";
  }
  p.exportedOn = null;
  state.results = null;
  state.files = null;
  save.plan(); save.results();
  $("banners").replaceChildren();
  renderPages(); renderResults(); renderJourney(); renderDock();
  scheduleExtract();
  if (now) window.scrollTo({ top: 0 });
  toast(`Day ${p.day}` + (p.mode === "pdf" ? `: pages ${p.first} to ${p.last}.` : "."));
}

// ---------- settings ----------
function openSettings(message) {
  const s = state.settings;
  const cfg = state.config || {};
  $("s-passcode").value = s.passcode;
  $("s-key").value = s.apiKey;
  $("s-ppd").value = s.ppd;
  $("s-days").value = s.days;
  $("s-mcq-deck").value = s.mcqDeck || (cfg.decks && cfg.decks.mcq) || "";
  $("s-cloze-deck").value = s.clozeDeck || (cfg.decks && cfg.decks.cloze) || "";
  $("s-model").value = s.model || cfg.default_model || "";
  $("passcode-field").hidden = !cfg.passcode_required;
  $("key-hint").textContent = cfg.server_key
    ? "Your own key is optional; without it the app uses the server's key. Both are kept on this device only."
    : "This server has no key of its own, so add yours. It is kept on this device only.";
  const msg = $("settings-msg");
  msg.hidden = !message;
  msg.textContent = message || "";
  $("settings").showModal();
}

function closeSettings() {
  const s = state.settings;
  const cfg = state.config || {};
  const decksBefore = `${s.mcqDeck}|${s.clozeDeck}`;
  s.passcode = $("s-passcode").value.trim();
  s.apiKey = $("s-key").value.trim();
  s.ppd = Math.min(60, Math.max(1, Number($("s-ppd").value) || 7));
  s.days = Math.min(999, Math.max(1, Number($("s-days").value) || 85));
  const mcqDeck = $("s-mcq-deck").value.trim();
  const clozeDeck = $("s-cloze-deck").value.trim();
  s.mcqDeck = mcqDeck && cfg.decks && mcqDeck !== cfg.decks.mcq ? mcqDeck : "";
  s.clozeDeck = clozeDeck && cfg.decks && clozeDeck !== cfg.decks.cloze ? clozeDeck : "";
  const model = $("s-model").value.trim();
  s.model = model && model !== cfg.default_model ? model : "";
  save.settings();
  renderJourney();
  if (`${s.mcqDeck}|${s.clozeDeck}` !== decksBefore && state.results) refreshFiles();
}

// ---------- events ----------
function bind() {
  $("open-settings").addEventListener("click", () => openSettings());
  $("settings").addEventListener("close", closeSettings);
  $("settings").addEventListener("click", (e) => { if (e.target === $("settings")) $("settings").close(); });
  $("forget-book").addEventListener("click", async () => {
    await BookStore.clear();
    if (pdfDoc) { pdfDoc.destroy(); pdfDoc = null; }
    state.book = null;
    renderBook(); renderJourney(); scheduleExtract();
    toast("The saved book was removed from this device.");
  });

  $("day-down").addEventListener("click", () => { state.plan.day = Math.max(1, state.plan.day - 1); save.plan(); renderJourney(); });
  $("day-up").addEventListener("click", () => { state.plan.day = Math.min(state.settings.days, state.plan.day + 1); save.plan(); renderJourney(); });

  $("chapter").addEventListener("input", (e) => { state.plan.chapter = e.target.value; save.plan(); });

  document.querySelectorAll('input[name="mode"]').forEach((r) => r.addEventListener("change", (e) => {
    state.plan.mode = e.target.value; save.plan(); renderMode(); renderJourney(); scheduleExtract();
  }));

  $("book-input").addEventListener("change", async (e) => {
    const file = e.target.files && e.target.files[0];
    e.target.value = "";
    if (!file) return;
    setStatus("Opening the PDF…");
    try {
      const data = await file.arrayBuffer();
      const pages = await openPdf(data);
      state.book = { name: file.name, size: file.size, pages };
      const stored = await BookStore.put({ name: file.name, size: file.size, pages, data });
      if (!stored) toast("The book couldn't be saved on this device; you'll need to choose it again next time.");
    } catch {
      setStatus("That file couldn't be opened as a PDF.", true);
      return;
    }
    renderBook(); clampPages(); renderPages(); renderJourney(); save.plan(); scheduleExtract();
  });

  const setPage = (which, value) => {
    const p = state.plan;
    if (which === "first") {
      const span = p.last - p.first;
      p.first = value;
      p.last = value + span;                     // moving the start moves the whole window
    } else {
      p.last = value;
    }
    clampPages(); save.plan(); renderPages(); renderJourney(); scheduleExtract();
  };
  for (const id of ["first", "last"]) {
    $(id).addEventListener("change", (e) => setPage(id, Number(e.target.value)));
  }
  document.querySelectorAll("[data-num]").forEach((b) => b.addEventListener("click", () => {
    const which = b.dataset.num;
    setPage(which, state.plan[which] + Number(b.dataset.d));
  }));

  let pasteTimer;
  $("paste").addEventListener("input", (e) => {
    state.pasted = e.target.value;
    clearTimeout(pasteTimer);
    pasteTimer = setTimeout(() => LS.set("pasted", state.pasted), 400);
    scheduleExtract();
  });

  $("mcq-seg").addEventListener("change", (e) => { state.opts.mcq = e.target.value; save.opts(); renderOptions(); });
  $("cloze-seg").addEventListener("change", (e) => { state.opts.cloze = Number(e.target.value); save.opts(); });
  document.querySelectorAll('input[name="scope"]').forEach((r) => r.addEventListener("change", (e) => {
    state.opts.scope = e.target.value; save.opts();
  }));
  document.querySelectorAll('input[name="tab"]').forEach((r) => r.addEventListener("change", (e) => {
    state.tab = e.target.value; state.editing = null; renderResults();
  }));

  $("generate").addEventListener("click", generate);
  $("regenerate").addEventListener("click", generate);
  $("send-both").addEventListener("click", () => sendFile("both"));
  $("send-mcq").addEventListener("click", () => sendFile("mcq"));
  $("send-cloze").addEventListener("click", () => sendFile("cloze"));
}

// ---------- start ----------
async function start() {
  bind();
  $("chapter").value = state.plan.chapter;
  $("paste").value = state.pasted;
  renderMode(); renderPages(); renderBook(); renderJourney();

  try {
    state.config = await api("/api/config");
  } catch (e) {
    $("banners").replaceChildren(banner("error", `Couldn't load the app settings: ${e.message}`));
  }
  renderOptions();

  // A new calendar day after sending cards: move on to the next reading.
  const advanced = state.plan.exportedOn && state.plan.exportedOn !== today();

  const rec = await BookStore.get();
  if (rec && rec.data) {
    try {
      const pages = await openPdf(rec.data);
      state.book = { name: rec.name, size: rec.size, pages };
    } catch { await BookStore.clear(); }
  }
  renderBook();
  if (advanced) advance();
  else {
    clampPages(); renderPages();
    await extract();
    if (state.results) {
      // Generation interrupted by a reload: nothing is still running.
      for (const k of ["mcq", "cloze"]) {
        if (state.results[k] && state.results[k].pending) state.results[k] = { error: "Interrupted before it finished." };
      }
      renderResults();
      refreshFiles();
    }
  }
  renderDock();
}

start();
