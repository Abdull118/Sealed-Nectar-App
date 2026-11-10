// Sealed Nectar study cards: page logic.
// Plain ES module, no build step: Vercel serves it straight from public/.
// Three views: Today (make cards from the day's pages), Study (spaced
// repetition) and Cards (browse and edit). The book and the cards live on the
// server; only settings and an unsaved review are kept on the device.

const $ = (id) => document.getElementById(id);
const TZ = (() => { try { return Intl.DateTimeFormat().resolvedOptions().timeZone; } catch { return "UTC"; } })();
const LEARN_AHEAD_MS = 20 * 60 * 1000;

// ---------- storage on this device (guarded: private mode and full disks throw) ----------
const LS = {
  get(k, d) { try { const v = localStorage.getItem("sn:" + k); return v ? JSON.parse(v) : d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem("sn:" + k, JSON.stringify(v)); } catch { /* unavailable */ } },
  del(k) { try { localStorage.removeItem("sn:" + k); } catch { /* unavailable */ } },
};

// ---------- state ----------
const state = {
  config: null,
  ready: false,                // server state loaded (passcode accepted, storage present)
  settings: { passcode: "", apiKey: "", model: "", ppd: 7, days: 85, newLimit: 0, ...LS.get("settings", {}) },
  plan: { day: 1, chapter: "", first: 1, last: 7, mode: "book", saved_on: null, ...LS.get("plan", {}) },
  opts: { mcq: "auto", cloze: 10, ...LS.get("opts", {}) },
  pasted: LS.get("pasted", ""),
  book: null,                  // server summary
  pages: [],                   // [{pdf, printed, picture, text}]
  text: "",
  words: 0,
  textError: "",
  results: LS.get("results.v2", null),
  editing: null,
  tab: "mcq",
  busy: false,
  view: "today",
  study: { home: null, place: "all", session: null, loading: false },
  cards: { groups: null, place: "all", q: "", open: null, editing: null, confirm: null, limit: 150 },
};

if (!["book", "paste"].includes(state.plan.mode)) state.plan.mode = "book";   // older versions used "pdf"

const today = () => new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 10);

// ---------- tiny DOM helpers ----------
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
  toastTimer = setTimeout(() => t.classList.remove("show"), 4000);
}

function banner(kind, ...lines) {
  return el("div", { class: `banner ${kind}`, role: kind === "error" ? "alert" : null },
    ...lines.map((l) => (typeof l === "string" ? el("p", { text: l }) : el("p", {}, l))));
}

function showBanners(...nodes) { $("banners").replaceChildren(...nodes); }

// ---------- API ----------
async function api(path, { method, body, ownKey = false } = {}) {
  const headers = { "Content-Type": "application/json" };
  if (state.settings.passcode) headers["X-App-Passcode"] = state.settings.passcode;
  if (ownKey && state.settings.apiKey) headers["X-OpenAI-Key"] = state.settings.apiKey;
  let res;
  try {
    res = await fetch(path, { method: method || (body ? "POST" : "GET"), headers,
      body: body ? JSON.stringify(body) : undefined });
  } catch {
    throw Object.assign(new Error("Couldn't reach the server. Check your connection and try again."), { status: 0 });
  }
  let data = null;
  try { data = await res.json(); } catch { /* non-JSON error page */ }
  if (!res.ok) {
    let msg = `Request failed (${res.status}).`;
    if (res.status === 504) msg = "The server stopped after 5 minutes. Try fewer cards or a faster model in Settings.";
    else if (res.status === 413) msg = "That is too much to send at once. Choose fewer pages.";
    else if (data && typeof data.detail === "string") msg = data.detail;
    else if (data && Array.isArray(data.detail)) msg = data.detail.map((d) => d.msg).join("; ");
    if (res.status === 401) onLockedOut();
    throw Object.assign(new Error(msg), { status: res.status });
  }
  return data;
}

function onLockedOut() {
  state.ready = false;
  if (!$("settings").open) openSettings("Enter the passcode to use your book and cards.");
}

const storageMissing = () => state.config && state.config.storage === "missing";

function storageBanner() {
  return banner("warn",
    "The book and your cards need storage, which isn't connected yet.",
    "In Vercel open this project, then Storage, Create Database, Upstash for Redis (free plan), and connect it to the project. Redeploy, then reload this page.");
}

// ---------- plan (synced to the server) ----------
let planTimer;
function savePlan() {
  LS.set("plan", state.plan);
  if (!state.ready) return;
  clearTimeout(planTimer);
  planTimer = setTimeout(() => api("/api/plan", { method: "PUT", body: state.plan }).catch(() => {}), 500);
}

function advance(now = false) {
  const p = state.plan;
  const span = Math.max(1, Number(state.settings.ppd) || 7);
  p.day = Math.min(p.day + 1, Math.max(1, state.settings.days));
  if (p.mode === "book") {
    p.first = p.last + 1;
    p.last = p.first + span - 1;
    if (state.book && state.book.last_printed) {
      p.first = Math.min(p.first, state.book.last_printed);
      p.last = Math.min(p.last, state.book.last_printed);
    }
  } else {
    state.pasted = ""; LS.set("pasted", ""); $("paste").value = "";
  }
  p.saved_on = null;
  state.results = null; LS.del("results.v2");
  savePlan();
  showBanners();
  renderToday(); scheduleText();
  if (now) window.scrollTo({ top: 0 });
  toast(`Day ${p.day}` + (p.mode === "book" ? `: pages ${p.first} to ${p.last}.` : "."));
}

// =====================================================================
// TODAY
// =====================================================================

function renderJourney() {
  const { day } = state.plan;
  const total = Math.max(1, state.settings.days);
  $("day-title").replaceChildren(`Day ${day} `, el("small", { text: `of ${total}` }));
  const frac = total > 1 ? Math.min(1, (day - 1) / (total - 1)) : 1;
  $("route-walked").style.strokeDashoffset = String(1000 * (1 - frac));
  const path = $("route-path");
  const pt = path.getPointAtLength(path.getTotalLength() * frac);
  $("route-here").setAttribute("cx", pt.x.toFixed(1));
  $("route-here").setAttribute("cy", pt.y.toFixed(1));
  const p = state.plan;
  $("pages-label").textContent = p.mode === "book" ? `Pages ${p.first} to ${p.last}` : "";
}

function renderReading() {
  const p = state.plan;
  for (const r of document.querySelectorAll('input[name="mode"]')) r.checked = r.value === p.mode;
  $("book-box").hidden = p.mode !== "book";
  $("paste-box").hidden = p.mode !== "paste";
  $("first").value = p.first;
  $("last").value = p.last;
  $("no-book").hidden = !(state.ready && !state.book && p.mode === "book");

  const status = $("extract-status");
  status.classList.toggle("error", !!state.textError);
  const mapping = $("mapping");
  if (p.mode === "book") {
    if (!state.ready) { status.textContent = ""; mapping.textContent = "Use the page numbers printed in the book."; }
    else if (!state.book) { status.textContent = ""; mapping.textContent = "Add the book to pick pages by their printed numbers."; }
    else if (state.textError) { status.textContent = state.textError; mapping.textContent = ""; }
    else if (state.pages.length) {
      const pdfFirst = state.pages[0].pdf, pdfLast = state.pages[state.pages.length - 1].pdf;
      const pics = state.pages.filter((x) => x.picture).length;
      mapping.textContent = `PDF pages ${pdfFirst} to ${pdfLast}` +
        (pics ? `, including ${pics} picture page${pics > 1 ? "s" : ""}.` : ".");
      status.textContent = `${state.words.toLocaleString()} words.`;
    } else { status.textContent = "Loading the pages…"; }
  } else {
    status.textContent = state.words ? `${state.words.toLocaleString()} words.` : "";
  }

  const preview = $("preview");
  preview.hidden = !state.words;
  const box = $("preview-text");
  if (p.mode === "book" && state.pages.length) {
    box.replaceChildren(...state.pages.flatMap((pg) => [
      el("span", { class: "page-sep", text: pg.picture ? "Picture page" : `Page ${pg.printed}` }),
      pg.text,
    ]));
  } else {
    box.textContent = state.text.slice(0, 8000) + (state.text.length > 8000 ? " …" : "");
  }
}

function suggestCount(words) {
  for (const [limit, n] of state.config.suggest) if (limit === null || words <= limit) return n;
  return 20;
}
const mcqCount = () => (state.opts.mcq === "auto" ? suggestCount(state.words || 3000) : Number(state.opts.mcq));

function segOptions(container, name, options, current) {
  container.replaceChildren(...options.map(([value, label]) =>
    el("label", {}, el("input", { type: "radio", name, value: String(value), checked: String(value) === String(current) }),
      el("span", { text: label }))));
}

function renderOptions() {
  if (!state.config) return;
  const auto = suggestCount(state.words || 3000);
  segOptions($("mcq-seg"), "mcq", [["auto", `Auto ${auto}`], ...state.config.mcq_counts.map((n) => [n, String(n)])], state.opts.mcq);
  segOptions($("cloze-seg"), "cloze", state.config.cloze_max.map((n) => [n, String(n)]), state.opts.cloze);
  const s = state.config.splits[String(mcqCount())];
  $("mix").textContent = `Mixed difficulty, about ${s.Easy} easy, ${s.Medium} medium and ${s.Hard} hard at most.`;
}

function renderToday() { renderJourney(); renderReading(); renderOptions(); renderResults(); renderAction(); }

// ----- text for the selected pages -----
let textTimer, textSeq = 0;
function scheduleText() { clearTimeout(textTimer); textTimer = setTimeout(loadText, 300); }

async function loadText() {
  const seq = ++textSeq;
  const p = state.plan;
  state.textError = "";
  if (p.mode === "paste") {
    state.pages = []; state.text = state.pasted;
  } else if (!state.ready || !state.book) {
    state.pages = []; state.text = "";
  } else {
    state.pages = [];
    renderReading();
    try {
      const r = await api(`/api/book/text?first=${p.first}&last=${p.last}`);
      if (seq !== textSeq) return;
      state.pages = r.pages;
      state.text = r.pages.map((x) => x.text).join("\n\n");
    } catch (e) {
      if (seq !== textSeq) return;
      state.textError = e.message; state.text = "";
    }
  }
  state.words = (state.text.match(/\S+/g) || []).length;
  renderReading(); renderOptions(); renderAction();
}

function selectionSig() {
  const p = state.plan;
  return p.mode === "book" ? `book|${p.first}|${p.last}|${state.words}` : `paste|${state.words}`;
}

// ----- review of freshly generated cards -----
function clozeNodes(text, hideNumber = null) {
  const out = [];
  const re = /\{\{c(\d+)::(.*?)(?:::(.*?))?\}\}/g;
  let last = 0, m;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    if (hideNumber !== null && Number(m[1]) === hideNumber) out.push(el("span", { class: "gap", text: m[3] ? `[${m[3]}]` : "[...]" }));
    else if (hideNumber !== null) out.push(m[2]);
    else out.push(el("mark", {}, m[2], el("sup", { text: m[1] })));
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
      LS.set("results.v2", state.results); renderCounts(); renderAction();
    } }));
}

const detailsBlock = (summary, ...kids) => el("details", {}, el("summary", { text: summary }), ...kids);
const field = (label, node) => el("label", { class: "field" }, el("span", { text: label }), node);

function mcqForm(item, onSave, onCancel) {
  const f = el("form", { class: "edit-form", onsubmit: (e) => {
    e.preventDefault();
    const d = new FormData(f);
    onSave({ question: d.get("question").trim(), options: ["A", "B", "C", "D"].map((L) => d.get(L).trim()),
      answer: d.get("answer"), context: d.get("context").trim() });
  } });
  f.append(field("Question", el("textarea", { name: "question", required: true }, item.question)));
  ["A", "B", "C", "D"].forEach((L, i) => f.append(field(`Option ${L}`, el("input", { type: "text", name: L, value: item.options[i], required: true }))));
  f.append(field("Correct option", el("select", { name: "answer" },
    ...["A", "B", "C", "D"].map((L) => el("option", { value: L, selected: L === item.answer, text: L })))));
  f.append(field("Context", el("textarea", { name: "context" }, item.context || "")));
  f.append(el("div", { class: "row" },
    el("button", { type: "button", class: "btn small", text: "Cancel", onclick: onCancel }),
    el("button", { type: "submit", class: "btn small primary", text: "Save" })));
  return f;
}

function clozeForm(item, onSave, onCancel) {
  const err = el("p", { class: "flag-note", hidden: true });
  const f = el("form", { class: "edit-form", onsubmit: async (e) => {
    e.preventDefault();
    const d = new FormData(f);
    const text = d.get("text").trim();
    if (!/\{\{c\d+::[^{}]+?\}\}/.test(text)) {
      err.textContent = "Keep at least one deletion written as {{c1::answer}}.";
      err.hidden = false;
      return;
    }
    try { await onSave({ text, extra: d.get("extra").trim() }); }
    catch (ex) { err.textContent = ex.message; err.hidden = false; }
  } });
  f.append(field("Card text", el("textarea", { name: "text", required: true, spellcheck: "false" }, item.text)));
  f.append(err);
  f.append(field("Background", el("textarea", { name: "extra" }, item.extra || "")));
  f.append(el("div", { class: "row" },
    el("button", { type: "button", class: "btn small", text: "Cancel", onclick: onCancel }),
    el("button", { type: "submit", class: "btn small primary", text: "Save" })));
  return f;
}

function reviewCard(kind, item, index) {
  const editing = state.editing && state.editing.kind === kind && state.editing.index === index;
  const li = el("li", { class: `card${item.verified ? "" : " flagged"}${item.keep ? "" : " dropped"}` });
  const tags = kind === "mcq"
    ? [el("span", { class: `tag ${item.difficulty.toLowerCase()}`, text: item.difficulty }), el("span", { class: "tag", text: item.place })]
    : [el("span", { class: "tag", text: item.category }), el("span", { class: "tag", text: item.place })];
  li.append(el("div", { class: "card-head" }, ...tags, el("span", { class: "spacer" }), keepSwitch(item)));
  const cancel = () => { state.editing = null; renderResults(); };
  const save = (changes) => { Object.assign(item, changes); state.editing = null; LS.set("results.v2", state.results); renderResults(); };
  if (editing) {
    li.append(kind === "mcq" ? mcqForm(item, save, cancel) : clozeForm(item, save, cancel));
    return li;
  }
  if (kind === "mcq") {
    li.append(el("p", { class: "q", text: item.question }));
    li.append(el("ol", { class: "opts" }, ...item.options.map((o, i) => {
      const L = "ABCD"[i];
      return el("li", { class: L === item.answer ? "correct" : null }, el("b", { text: L }), el("span", { text: o }),
        L === item.answer ? el("span", { class: "sr-only", text: " (correct)" }) : null);
    })));
  } else {
    li.append(el("p", { class: "cloze" }, ...clozeNodes(item.text)));
  }
  if (!item.verified) li.append(el("p", { class: "flag-note", text: `Check this one: ${item.check_notes}.` }));
  const more = kind === "mcq" ? item.context : item.extra;
  li.append(detailsBlock(kind === "mcq" ? "Context and source" : "Background and source",
    more ? el("p", { text: more }) : null, item.source_excerpt ? el("blockquote", { text: item.source_excerpt }) : null));
  li.append(el("div", { class: "card-actions" },
    el("button", { type: "button", class: "link-btn", text: "Edit", onclick: () => { state.editing = { kind, index }; renderResults(); } })));
  return li;
}

function pendingBlock(kind) {
  const r = state.results;
  const secs = Math.round((Date.now() - r.startedAt) / 1000);
  const what = kind === "mcq" ? `Writing up to ${r.mcq.n} questions` : "Writing fill-in cards";
  return el("div", { class: "pending" }, el("div", { class: "spinner", "aria-hidden": "true" }),
    el("span", { text: `${what}… ${secs} s. Keep this screen open.` }));
}

function renderTab(kind) {
  const box = $(`tab-${kind}`);
  const r = state.results && state.results[kind];
  if (!r) { box.replaceChildren(); return; }
  const kids = [];
  if (r.pending) kids.push(pendingBlock(kind));
  else if (r.error) kids.push(banner("error", r.error, "Tap Generate again to retry."));
  else {
    for (const w of r.warnings || []) kids.push(banner("warn", w));
    if (r.items.length) kids.push(el("ol", { class: "cards" }, ...r.items.map((it, i) => reviewCard(kind, it, i))));
  }
  box.replaceChildren(...kids);
}

function renderCounts() {
  for (const kind of ["mcq", "cloze"]) {
    const r = state.results && state.results[kind];
    const c = $(`${kind}-count`);
    c.classList.remove("flagged");
    if (!r || r.pending) { c.textContent = r ? "working" : ""; continue; }
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
  renderTab("mcq"); renderTab("cloze"); renderCounts();
}

const keptItems = (kind) => {
  const r = state.results && state.results[kind];
  return r && r.items ? r.items.filter((i) => i.keep) : [];
};

async function generate() {
  if (state.busy || !state.config) return;
  const chapter = $("chapter").value.trim();
  if (!chapter) { showBanners(banner("error", "Add a chapter name first. It becomes the card's tag.")); $("chapter").focus(); return; }
  if (state.words < 100) {
    showBanners(banner("error", state.plan.mode === "book"
      ? "Choose pages with at least 100 words of text." : "Paste at least 100 words of text."));
    return;
  }
  const cfg = state.config;
  if (!state.settings.apiKey && !cfg.server_key) return openSettings("Add your OpenAI key to generate cards.");
  if (!state.settings.apiKey && cfg.passcode_required && !state.settings.passcode) return openSettings("Enter the passcode to generate cards.");

  showBanners();
  state.busy = true; state.editing = null; state.tab = "mcq";
  const n = mcqCount();
  state.results = { chapter, day: state.plan.day, sig: selectionSig(), startedAt: Date.now(),
    printed: state.plan.mode === "book" ? [state.plan.first, state.plan.last] : null,
    mcq: { pending: true, n }, cloze: { pending: true } };
  renderResults(); renderAction();
  $("results").scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });

  let lock = null;
  try { lock = await navigator.wakeLock?.request("screen"); } catch { /* not supported */ }
  const tick = setInterval(() => { for (const k of ["mcq", "cloze"]) if (state.results[k].pending) renderTab(k); }, 1000);
  const body = { text: state.text, chapter, n_mcq: n, max_cloze: Number(state.opts.cloze), model: state.settings.model || null };
  const run = (kind) => api(`/api/generate/${kind}`, { body, ownKey: true })
    .then((d) => { state.results[kind] = { items: d.items, warnings: d.warnings }; })
    .catch((e) => { state.results[kind] = { error: e.message }; })
    .finally(() => { renderTab(kind); renderCounts(); });
  await Promise.all([run("mcq"), run("cloze")]);
  clearInterval(tick);
  try { await lock?.release(); } catch { /* already released */ }
  state.busy = false;
  LS.set("results.v2", state.results);
  renderResults(); renderAction();
}

async function saveCards() {
  const r = state.results;
  const mcq = keptItems("mcq"), cloze = keptItems("cloze");
  if (!mcq.length && !cloze.length) return;
  state.busy = true; renderAction();
  try {
    const d = await api("/api/cards", { body: { chapter: r.chapter, day: r.day, printed: r.printed, mcq, cloze } });
    state.results = null; LS.del("results.v2");
    state.plan.saved_on = today(); savePlan();
    const extra = [d.duplicates ? `${d.duplicates} already in your deck` : "", d.invalid ? `${d.invalid} invalid` : ""].filter(Boolean).join(", ");
    toast(`Saved ${d.saved_cards} card${d.saved_cards === 1 ? "" : "s"}${extra ? ` (${extra} skipped)` : ""}.`);
    showBanners(el("div", { class: "banner warn" },
      el("p", { text: "Saved to your deck. Tomorrow opens on the next day and pages." }),
      el("p", {}, el("button", { type: "button", class: "link-btn", text: "Study now", onclick: () => setView("study") }),
        "  ", el("button", { type: "button", class: "link-btn", text: "Set up the next reading", onclick: () => advance(true) }))));
    state.cards.groups = null;
    refreshStudyCounts();
  } catch (e) {
    showBanners(banner("error", `The cards weren't saved: ${e.message}`));
  }
  state.busy = false;
  renderResults(); renderAction();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

// =====================================================================
// BOOK (Settings)
// =====================================================================
let pdfjs = null;

async function openPdf(data) {
  if (!pdfjs) {
    pdfjs = await import("/vendor/pdfjs/pdf.min.mjs");
    pdfjs.GlobalWorkerOptions.workerSrc = "/vendor/pdfjs/pdf.worker.min.mjs";
  }
  const task = pdfjs.getDocument({ data: new Uint8Array(data) });
  const doc = await task.promise;
  doc.closeTask = () => task.destroy();             // pdf.js 6 frees memory through the loading task
  return doc;
}

async function pageText(doc, n) {
  const page = await doc.getPage(n);
  const content = await page.getTextContent();
  let out = "", prev = null;
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
  page.cleanup();
  return out;
}

function bookProgress(frac, text) {
  $("book-progress").hidden = frac === null;
  if (frac !== null) {
    $("book-progress-bar").style.width = `${Math.round(frac * 100)}%`;
    $("book-progress-text").textContent = text;
  }
}

async function uploadBook(file) {
  if (!state.ready) { $("settings-msg").hidden = false; $("settings-msg").textContent = "Enter the passcode first."; return; }
  let doc = null;
  try {
    bookProgress(0, "Opening the PDF…");
    doc = await openPdf(await file.arrayBuffer());
    const pages = doc.numPages;
    let labels = null;
    try { labels = await doc.getPageLabels(); } catch { /* none */ }
    const { version, chunk } = await api("/api/book/begin", { body: { name: file.name, pages, labels } });
    for (let start = 0; start < pages; start += chunk) {
      const texts = [];
      for (let i = start; i < Math.min(pages, start + chunk); i++) {
        texts.push(await pageText(doc, i + 1));
        bookProgress((i + 1) / pages * 0.95, `Reading page ${i + 1} of ${pages}…`);
      }
      const send = () => api("/api/book/pages", { body: { version, start, texts } });
      try { await send(); } catch { await send(); }       // one retry for a flaky connection
    }
    bookProgress(0.97, "Matching the printed page numbers…");
    state.book = await api("/api/book/finish", { body: { version } });
    bookProgress(null);
    renderBookSettings();
    toast("The book is saved. Pick pages by the numbers printed in it.");
    renderToday(); scheduleText();
  } catch (e) {
    bookProgress(null);
    $("settings-msg").hidden = false;
    $("settings-msg").textContent = `The book wasn't saved: ${e.message}`;
  } finally {
    if (doc) doc.closeTask();
  }
}

function renderBookSettings() {
  const b = state.book;
  const st = $("book-status");
  if (!state.ready) { st.textContent = storageMissing() ? "Connect storage in Vercel first." : "Enter the passcode to see the book."; }
  else if (!b) { st.textContent = "No book yet. Choose your PDF once; it's read on this device and only its text is saved."; }
  else {
    const how = b.method === "labels" ? "Page numbers come from the PDF's own labels."
      : b.method === "detected" ? `Page numbers were read from the pages; ${b.picture_pages} picture page${b.picture_pages === 1 ? "" : "s"} skipped.`
      : "Printed page numbers couldn't be read, so PDF page numbers are used. Use Check to fix one page and the rest follow.";
    st.replaceChildren(b.name, el("small", { text: `Printed pages ${b.first_printed} to ${b.last_printed} (${b.pdf_pages} PDF pages). ${how}` + (b.fixes ? ` ${b.fixes} manual fix${b.fixes > 1 ? "es" : ""}.` : "") }));
  }
  $("book-btn-label").textContent = b ? "Replace PDF" : "Choose PDF";
  $("forget-book").hidden = !b;
  $("book-check").hidden = !b;
}

async function checkPage() {
  const n = Number($("check-page").value);
  const out = $("check-result");
  if (!n) return;
  try {
    const r = await api(`/api/book/locate?printed=${n}`);
    const fixInput = el("input", { type: "number", inputmode: "numeric", min: "1", value: String(n), "aria-label": "Correct printed page" });
    out.replaceChildren(el("div", { class: "check-result" },
      el("p", { style: "margin:0 0 8px", text: `Printed page ${n} is PDF page ${r.pdf}. It begins:` }),
      el("q", { text: r.opening || "(no text on this page)" }),
      el("p", { style: "margin:10px 0 6px", text: "Different in your book? Enter the number printed on the page that begins like this:" }),
      el("div", { class: "num", style: "max-width:260px" }, fixInput,
        el("button", { type: "button", style: "width:auto;padding:0 14px;font-size:15px", text: "Fix", onclick: async () => {
          try {
            state.book = await api("/api/book/fix", { body: { pdf_page: r.pdf, printed: Number(fixInput.value) } });
            renderBookSettings();
            out.replaceChildren(el("p", { class: "check-result", text: "Fixed. Pages around it now follow the new numbering." }));
            scheduleText();
          } catch (e) { toast(e.message); }
        } }))));
  } catch (e) {
    out.replaceChildren(el("p", { class: "check-result", text: e.message }));
  }
}

// =====================================================================
// STUDY
// =====================================================================
const PLACE_LABELS = { all: "All", Makkah: "Makkah", Madinah: "Madinah", "Hijrah route": "Hijrah route", Other: "Other" };

async function refreshStudyCounts() {
  if (!state.ready) return;
  try {
    const nl = Number(state.settings.newLimit) || 0;
    const q = await api(`/api/study?place=all&tz=${encodeURIComponent(TZ)}${nl ? `&new_limit=${nl}` : ""}`);
    const n = q.items.length;
    const b = $("study-badge");
    b.hidden = !n; b.textContent = n > 99 ? "99+" : String(n);
    if (state.study.place === "all") state.study.home = q;
  } catch { /* badge is optional */ }
}

async function loadStudy() {
  if (!state.ready) { renderStudy(); return; }
  state.study.loading = true; renderStudy();
  try {
    const nl = Number(state.settings.newLimit) || 0;
    state.study.home = await api(`/api/study?place=${encodeURIComponent(state.study.place)}&tz=${encodeURIComponent(TZ)}${nl ? `&new_limit=${nl}` : ""}`);
  } catch (e) {
    showBanners(banner("error", e.message));
  }
  state.study.loading = false;
  renderStudy(); renderAction();
}

function relTime(iso) {
  const ms = Date.parse(iso) - Date.now();
  if (ms <= 60000) return "now";
  const m = Math.round(ms / 60000);
  if (m < 60) return `in ${m} min`;
  const h = Math.round(m / 60);
  if (h < 36) return `in ${h} h`;
  return `in ${Math.round(h / 24)} days`;
}

function renderStudy() {
  const s = state.study;
  $("study-home").hidden = !!s.session;
  $("study-session").hidden = !s.session;
  if (s.session) return renderSession();
  const h = s.home;
  if (!state.ready) {
    $("due-total").textContent = "–";
    $("due-label").textContent = storageMissing() ? "storage isn't connected yet" : "enter the passcode in Settings";
    $("due-break").textContent = ""; $("study-places").replaceChildren(); $("study-next").textContent = "";
    return;
  }
  if (!h) { $("due-total").textContent = "…"; return; }
  const items = h.items;
  const nNew = items.filter((i) => i.status === "new").length;
  $("due-total").textContent = String(items.length);
  $("due-label").textContent = items.length === 1 ? "card to study" : "cards to study";
  $("due-break").textContent = items.length
    ? `${items.length - nNew} review${items.length - nNew === 1 ? "" : "s"} and ${nNew} new. ${h.reviewed_today} studied today.`
    : `${h.reviewed_today} studied today.`;
  const places = ["all", ...state.config.places];
  $("study-places").replaceChildren(...places.map((p) => {
    const c = h.counts[p] || { due: 0, new: 0 };
    return el("label", {}, el("input", { type: "radio", name: "study-place", value: p, checked: p === s.place }),
      el("span", {}, PLACE_LABELS[p], el("small", { text: String(c.due + c.new) })));
  }));
  $("study-next").textContent = !items.length && h.next_due ? `Next card is due ${relTime(h.next_due)}.`
    : !items.length ? "Save cards from Today and they'll appear here." : "";
}

function startSession() {
  const h = state.study.home;
  if (!h || !h.items.length) return;
  state.study.session = { queue: h.items.map((x) => ({ ...x })), done: 0, current: null, revealed: false, chosen: null, pending: 0 };
  document.body.classList.add("in-session");
  window.scrollTo({ top: 0 });
  nextCard();
}

function endSession() {
  state.study.session = null;
  document.body.classList.remove("in-session");
  loadStudy(); refreshStudyCounts();
}

function nextCard() {
  const s = state.study.session;
  s.revealed = false; s.chosen = null;
  if (!s.queue.length) {
    s.current = null;
    renderStudy(); renderAction();
    return;
  }
  const now = Date.now();
  let idx = s.queue.findIndex((it) => it.status !== "learning" || Date.parse(it.due) <= now);
  if (idx < 0) {           // only learning cards left, none due yet: show the soonest early
    idx = s.queue.reduce((best, it, i) => (Date.parse(it.due) < Date.parse(s.queue[best].due) ? i : best), 0);
  }
  s.current = s.queue.splice(idx, 1)[0];
  renderStudy(); renderAction();
}

function reveal(letter = null) {
  const s = state.study.session;
  if (!s || !s.current || s.revealed) return;
  s.revealed = true; s.chosen = letter;
  renderStudy(); renderAction();
}

async function grade(rating) {
  const s = state.study.session;
  if (!s || !s.current || !s.revealed) return;
  const it = s.current;
  s.done++; s.pending++;
  nextCard();
  try {
    const upd = await api("/api/study/review", { body: { note_id: it.note_id, item: it.item, rating, tz: TZ } });
    if (upd.status === "learning" && Date.parse(upd.due) - Date.now() < LEARN_AHEAD_MS) s.queue.push(upd);
  } catch (e) {
    s.queue.push(it); s.done--;
    toast(`That answer wasn't saved (${e.message}). The card will come back.`);
  }
  s.pending--;
  if (state.study.session === s && !s.current) nextCard();
}

function renderSession() {
  const s = state.study.session;
  const left = s.queue.length + (s.current ? 1 : 0);
  $("session-left").textContent = left ? `${left} left` : "";
  $("session-meter").style.width = `${Math.round((s.done / Math.max(1, s.done + left)) * 100)}%`;
  const box = $("study-card");
  if (!s.current) {
    box.replaceChildren(s.pending
      ? el("div", { class: "pending" }, el("div", { class: "spinner" }), el("span", { text: "Saving…" }))
      : el("div", { class: "done-card" }, el("h2", { text: "Done for now" }),
        el("p", { text: `${s.done} card${s.done === 1 ? "" : "s"} studied.` }),
        el("button", { type: "button", class: "btn primary", text: "Finish", onclick: endSession })));
    return;
  }
  const it = s.current;
  const head = el("div", { class: "card-head" },
    el("span", { class: `tag${it.status === "new" ? " new" : ""}`, text: it.status === "new" ? "New" : it.status === "learning" ? "Learning" : "Review" }),
    el("span", { class: "tag", text: it.place }),
    it.type === "mcq" && it.difficulty ? el("span", { class: `tag ${it.difficulty.toLowerCase()}`, text: it.difficulty }) : null);
  const card = el("div", { class: "study-card" }, head);
  if (it.type === "mcq") {
    card.append(el("p", { class: "q", text: it.question }));
    it.options.forEach((o, i) => {
      const L = "ABCD"[i];
      let cls = "choice";
      if (s.revealed) cls += L === it.answer ? " right" : L === s.chosen ? " wrong" : " faded";
      card.append(el("button", { type: "button", class: cls, disabled: s.revealed, onclick: () => reveal(L) },
        el("b", { text: L }), el("span", { text: o })));
    });
  } else {
    card.append(el("p", { class: "cloze" }, ...(s.revealed ? clozeRevealed(it) : clozeNodes(it.text, it.cloze))));
  }
  if (s.revealed) {
    const more = it.type === "mcq" ? it.context : it.extra;
    card.append(el("div", { class: "answer-notes" },
      more ? el("p", { text: more }) : null,
      it.source_excerpt ? el("blockquote", { text: it.source_excerpt }) : null,
      it.chapter ? el("p", { class: "hint", style: "margin:8px 0 0", text: `${it.chapter}${it.day ? `, day ${it.day}` : ""}` }) : null));
  }
  box.replaceChildren(card);
}

function clozeRevealed(it) {
  const out = [];
  const re = /\{\{c(\d+)::(.*?)(?:::(.*?))?\}\}/g;
  let last = 0, m;
  while ((m = re.exec(it.text))) {
    if (m.index > last) out.push(it.text.slice(last, m.index));
    out.push(Number(m[1]) === it.cloze ? el("mark", { text: m[2] }) : m[2]);
    last = re.lastIndex;
  }
  out.push(it.text.slice(last));
  return out;
}

// =====================================================================
// CARDS
// =====================================================================
async function loadCards() {
  if (!state.ready) { renderCards(); return; }
  try { state.cards.groups = (await api(`/api/cards?tz=${encodeURIComponent(TZ)}`)).groups; }
  catch (e) { showBanners(banner("error", e.message)); }
  renderCards();
}

function noteText(n) { return n.type === "mcq" ? n.question : n.text.replace(/\{\{c\d+::(.*?)(?:::.*?)?\}\}/g, "$1"); }

function dueTag(n) {
  if (n.suspended) return el("span", { class: "tag", text: "Suspended" });
  if (n.new === n.cards) return el("span", { class: "tag new", text: "New" });
  if (!n.next_due) return null;
  const ms = Date.parse(n.next_due) - Date.now();
  return el("span", { class: `tag${ms < 86400000 ? " due" : ""}`, text: ms < 86400000 ? "Due today" : `Due ${relTime(n.next_due)}` });
}

function renderCards() {
  const c = state.cards;
  const list = $("cards-list");
  if (!state.ready) { $("cards-summary").textContent = storageMissing() ? "Connect storage in Vercel to save cards." : "Enter the passcode in Settings."; list.replaceChildren(); return; }
  if (!c.groups) { $("cards-summary").textContent = "Loading…"; return; }
  const q = c.q.trim().toLowerCase();
  const places = ["all", ...state.config.places];
  const all = c.groups.flatMap((g) => g.notes);
  $("cards-places").replaceChildren(...places.map((p) => el("label", {},
    el("input", { type: "radio", name: "cards-place", value: p, checked: p === c.place }),
    el("span", {}, PLACE_LABELS[p], el("small", { text: String(p === "all" ? all.length : all.filter((n) => n.place === p).length) })))));
  const match = (n) => (c.place === "all" || n.place === c.place) &&
    (!q || [noteText(n), ...(n.options || []), n.context || "", n.extra || ""].join(" ").toLowerCase().includes(q));
  let shown = 0;
  const blocks = [];
  for (const g of [...c.groups].reverse()) {
    const notes = g.notes.filter(match);
    if (!notes.length || shown >= c.limit) continue;
    const pages = g.printed ? `pp. ${g.printed[0]} to ${g.printed[1]}` : "";
    blocks.push(el("div", { class: "group-head" }, el("h3", { text: g.chapter || "Untitled" }),
      el("small", { text: [g.day ? `Day ${g.day}` : "", pages].filter(Boolean).join(", ") })));
    for (const n of notes) {
      if (shown++ >= c.limit) break;
      blocks.push(noteRow(n));
    }
  }
  const total = all.filter(match).length;
  $("cards-summary").textContent = all.length
    ? `${total} of ${all.length} cards` + (q || c.place !== "all" ? " match." : ".")
    : "No saved cards yet. Make some on Today.";
  if (total > c.limit) blocks.push(el("button", { type: "button", class: "btn full", text: "Show more", onclick: () => { c.limit += 150; renderCards(); } }));
  list.replaceChildren(...blocks);
}

function noteRow(n) {
  const c = state.cards;
  const open = c.open === n.id;
  const row = el("div", { class: `note-row${n.suspended ? " suspended" : ""}` });
  const head = el("button", { type: "button", "aria-expanded": String(open), onclick: () => { c.open = open ? null : n.id; c.editing = null; c.confirm = null; renderCards(); } },
    n.type === "mcq" ? el("span", { class: "q", style: "font-size:16px;margin:0", text: n.question })
      : el("span", { class: "cloze", style: "font-size:16px" }, ...clozeNodes(n.text)),
    el("span", { class: "meta" }, el("span", { class: "tag", text: n.type === "mcq" ? "Question" : "Fill-in" }),
      el("span", { class: "tag", text: n.place }), dueTag(n)));
  row.append(head);
  if (!open) return row;
  const body = el("div", { class: "body" });
  if (c.editing === n.id) {
    const done = async (changes) => {
      const upd = await api(`/api/cards/${encodeURIComponent(n.id)}`, { method: "PATCH", body: changes });
      Object.assign(n, upd); c.editing = null; renderCards(); toast("Saved.");
    };
    const cancel = () => { c.editing = null; renderCards(); };
    body.append(n.type === "mcq" ? mcqForm(n, (ch) => done(ch).catch((e) => toast(e.message)), cancel) : clozeForm(n, done, cancel));
  } else {
    if (n.type === "mcq") body.append(el("p", {}, `Answer: ${n.answer}. ${n.options["ABCD".indexOf(n.answer)]}`));
    const more = n.type === "mcq" ? n.context : n.extra;
    if (more) body.append(el("p", { text: more }));
    if (n.source_excerpt) body.append(el("blockquote", { style: "margin:0 0 10px;padding-left:10px;border-left:3px solid var(--honey);font-style:italic", text: n.source_excerpt }));
    body.append(el("div", { class: "actions" },
      el("button", { type: "button", class: "btn small", text: "Edit", onclick: () => { c.editing = n.id; renderCards(); } }),
      el("button", { type: "button", class: "btn small", text: n.suspended ? "Unsuspend" : "Suspend", onclick: async () => {
        try { Object.assign(n, await api(`/api/cards/${encodeURIComponent(n.id)}`, { method: "PATCH", body: { suspended: !n.suspended } })); renderCards(); refreshStudyCounts(); }
        catch (e) { toast(e.message); }
      } }),
      el("button", { type: "button", class: "btn small", style: c.confirm === n.id ? "color:var(--flag);border-color:var(--flag)" : null,
        text: c.confirm === n.id ? "Tap again to delete" : "Delete", onclick: async () => {
          if (c.confirm !== n.id) { c.confirm = n.id; renderCards(); return; }
          try {
            await api(`/api/cards/${encodeURIComponent(n.id)}`, { method: "DELETE" });
            for (const g of c.groups) g.notes = g.notes.filter((x) => x.id !== n.id);
            c.open = null; c.confirm = null; renderCards(); refreshStudyCounts(); toast("Deleted.");
          } catch (e) { toast(e.message); }
        } })));
  }
  row.append(body);
  return row;
}

// =====================================================================
// SHELL: views, action bar, settings
// =====================================================================
function setView(view) {
  state.view = view;
  for (const b of document.querySelectorAll(".tabbar button")) b.setAttribute("aria-selected", String(b.dataset.view === view));
  $("view-today").hidden = view !== "today";
  $("view-study").hidden = view !== "study";
  $("view-cards").hidden = view !== "cards";
  showBanners(...(storageMissing() ? [storageBanner()] : []));
  window.scrollTo({ top: 0 });
  if (view === "study") loadStudy();
  if (view === "cards") loadCards();
  renderAction();
}

function renderAction() {
  const a = $("action");
  const kids = [];
  if (state.view === "today") {
    const r = state.results;
    const done = r && !state.busy && ((r.mcq && r.mcq.items) || (r.cloze && r.cloze.items));
    if (done && r.sig === selectionSig()) {
      // one card per question, and one per deletion (c1, c2...) in a fill-in sentence
      const n = keptItems("mcq").length + keptItems("cloze").reduce((sum, it) =>
        sum + Math.max(1, new Set([...it.text.matchAll(/\{\{c(\d+)::/g)].map((m) => m[1])).size), 0);
      kids.push(el("button", { type: "button", class: "btn primary full", id: "save-cards", disabled: !n || !state.ready,
        text: !state.ready ? "Connect storage to save cards" : n ? `Save ${n} card${n === 1 ? "" : "s"} to my deck` : "Keep at least one card", onclick: saveCards }));
    } else {
      const label = state.busy ? (r && (r.mcq.pending || r.cloze.pending) ? "Generating…" : "Saving…")
        : done ? "Generate cards for these pages" : "Generate cards";
      kids.push(el("button", { type: "button", class: "btn primary full", id: "generate",
        disabled: state.busy || !state.config || state.words < 100, text: label, onclick: generate }));
    }
  } else if (state.view === "study") {
    const s = state.study.session;
    if (!s) {
      const n = state.study.home ? state.study.home.items.length : 0;
      kids.push(el("button", { type: "button", class: "btn primary full", disabled: !n, text: n ? `Study ${n} card${n === 1 ? "" : "s"}` : "Nothing due", onclick: startSession }));
    } else if (s.current && !s.revealed) {
      kids.push(el("button", { type: "button", class: "btn primary full", text: s.current.type === "mcq" ? "Show answer" : "Show answer", onclick: () => reveal(null) }));
    } else if (s.current) {
      const it = s.current;
      const suggest = it.type === "mcq" && s.chosen ? (s.chosen === it.answer ? 3 : 1) : null;
      const names = { 1: "Again", 2: "Hard", 3: "Good", 4: "Easy" };
      kids.push(el("div", { class: "grades" }, ...[1, 2, 3, 4].map((r) =>
        el("button", { type: "button", class: `grade${r === 1 ? " again" : ""}${suggest === r ? " suggest" : ""}`, onclick: () => grade(r) },
          names[r], el("small", { text: it.previews[String(r)] })))));
    }
  }
  a.replaceChildren(...kids);
}

function openSettings(message) {
  const s = state.settings;
  const cfg = state.config || {};
  $("s-passcode").value = s.passcode;
  $("s-key").value = s.apiKey;
  $("s-ppd").value = s.ppd;
  $("s-days").value = s.days;
  $("s-new").value = String(s.newLimit || 0);
  $("s-model").value = s.model || cfg.default_model || "";
  $("passcode-field").hidden = !cfg.passcode_required;
  $("key-hint").textContent = cfg.server_key
    ? "Your own key is optional; without it the app uses the server's key. Both are kept on this device only."
    : "This server has no OpenAI key of its own, so add yours. It is kept on this device only.";
  const msg = $("settings-msg");
  msg.hidden = !message; msg.textContent = message || "";
  $("check-result").replaceChildren();
  renderBookSettings();
  if (!$("settings").open) $("settings").showModal();
}

async function closeSettings() {
  const s = state.settings;
  const cfg = state.config || {};
  const passBefore = s.passcode;
  s.passcode = $("s-passcode").value.trim();
  s.apiKey = $("s-key").value.trim();
  s.ppd = Math.min(60, Math.max(1, Number($("s-ppd").value) || 7));
  s.days = Math.min(999, Math.max(1, Number($("s-days").value) || 85));
  s.newLimit = Number($("s-new").value) || 0;
  const model = $("s-model").value.trim();
  s.model = model && model !== cfg.default_model ? model : "";
  LS.set("settings", s);
  renderJourney();
  if (s.passcode !== passBefore || !state.ready) await loadServerState();
  if (state.view === "study") loadStudy();
}

async function exportAll() {
  try {
    const d = await api("/api/export/anki");
    if (!d.count) { toast("There are no cards to export yet."); return; }
    const file = new File([d.csv], d.name, { type: "text/csv" });
    if (matchMedia("(pointer: coarse)").matches && navigator.canShare && navigator.canShare({ files: [file] })) {
      try { await navigator.share({ files: [file] }); return; } catch (e) { if (e && e.name === "AbortError") return; }
    }
    const url = URL.createObjectURL(file);
    const a = el("a", { href: url, download: d.name });
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
    toast(`Saved ${d.name} with ${d.count} cards.`);
  } catch (e) { toast(e.message); }
}

// =====================================================================
// EVENTS AND START
// =====================================================================
function bind() {
  $("open-settings").addEventListener("click", () => openSettings());
  $("settings").addEventListener("close", closeSettings);
  $("settings").addEventListener("click", (e) => { if (e.target === $("settings")) $("settings").close(); });
  $("no-book-btn").addEventListener("click", () => openSettings());
  $("book-input").addEventListener("change", (e) => {
    const file = e.target.files && e.target.files[0];
    e.target.value = "";
    if (file) uploadBook(file);
  });
  $("forget-book").addEventListener("click", async (e) => {
    const b = e.currentTarget;
    if (b.dataset.confirm !== "1") { b.dataset.confirm = "1"; b.textContent = "Tap again to remove"; return; }
    b.dataset.confirm = ""; b.textContent = "Remove";
    try { await api("/api/book", { method: "DELETE" }); state.book = null; renderBookSettings(); renderToday(); scheduleText(); }
    catch (err) { toast(err.message); }
  });
  $("check-go").addEventListener("click", checkPage);
  $("export-all").addEventListener("click", exportAll);

  for (const b of document.querySelectorAll(".tabbar button")) b.addEventListener("click", () => setView(b.dataset.view));

  $("day-down").addEventListener("click", () => { state.plan.day = Math.max(1, state.plan.day - 1); savePlan(); renderJourney(); });
  $("day-up").addEventListener("click", () => { state.plan.day = Math.min(state.settings.days, state.plan.day + 1); savePlan(); renderJourney(); });
  $("chapter").addEventListener("input", (e) => { state.plan.chapter = e.target.value; savePlan(); });
  document.querySelectorAll('input[name="mode"]').forEach((r) => r.addEventListener("change", (e) => {
    state.plan.mode = e.target.value; savePlan(); renderJourney(); renderReading(); scheduleText();
  }));

  const setPage = (which, value) => {
    const p = state.plan;
    value = Math.max(1, Math.round(value) || 1);
    if (which === "first") { const span = Math.max(0, p.last - p.first); p.first = value; p.last = value + span; }
    else p.last = Math.max(p.first, value);
    if (p.last - p.first > 79) p.last = p.first + 79;
    savePlan(); renderJourney(); renderReading(); scheduleText();
  };
  for (const id of ["first", "last"]) $(id).addEventListener("change", (e) => setPage(id, Number(e.target.value)));
  document.querySelectorAll("[data-num]").forEach((b) => b.addEventListener("click", () => setPage(b.dataset.num, state.plan[b.dataset.num] + Number(b.dataset.d))));

  let pasteTimer;
  $("paste").addEventListener("input", (e) => {
    state.pasted = e.target.value;
    clearTimeout(pasteTimer);
    pasteTimer = setTimeout(() => LS.set("pasted", state.pasted), 400);
    scheduleText();
  });

  $("mcq-seg").addEventListener("change", (e) => { state.opts.mcq = e.target.value; LS.set("opts", state.opts); renderOptions(); });
  $("cloze-seg").addEventListener("change", (e) => { state.opts.cloze = Number(e.target.value); LS.set("opts", state.opts); });
  document.querySelectorAll('input[name="tab"]').forEach((r) => r.addEventListener("change", (e) => { state.tab = e.target.value; state.editing = null; renderResults(); }));
  $("regenerate").addEventListener("click", generate);

  $("study-places").addEventListener("change", (e) => { state.study.place = e.target.value; loadStudy(); });
  $("end-session").addEventListener("click", endSession);
  $("card-search").addEventListener("input", (e) => { state.cards.q = e.target.value; state.cards.limit = 150; renderCards(); });
  $("cards-places").addEventListener("change", (e) => { state.cards.place = e.target.value; state.cards.limit = 150; renderCards(); });

  document.addEventListener("keydown", (e) => {
    const s = state.study.session;
    if (!s || !s.current || e.target.closest("input, textarea, select")) return;
    if (!s.revealed) {
      const L = e.key.toUpperCase();
      if (s.current.type === "mcq" && "ABCD".includes(L) && L.length === 1) { e.preventDefault(); reveal(L); }
      else if (e.key === " " || e.key === "Enter") { e.preventDefault(); reveal(null); }
    } else if ("1234".includes(e.key) && e.key.length === 1) { e.preventDefault(); grade(Number(e.key)); }
    else if (e.key === " " || e.key === "Enter") { e.preventDefault(); grade(3); }
  });
}

async function loadServerState() {
  state.ready = false;
  if (!state.config || storageMissing()) { renderAll(); return; }
  if (state.config.passcode_required && !state.settings.passcode) { renderAll(); return; }
  try {
    const st = await api("/api/state");
    state.ready = true;
    state.book = st.book;
    if (st.plan) state.plan = { ...state.plan, ...st.plan };
    else savePlan();                                   // first run: store this device's plan
  } catch (e) {
    if (e.status !== 401) showBanners(banner("error", e.message));
  }
  if (state.ready && state.plan.saved_on && state.plan.saved_on !== today()) advance();
  $("chapter").value = state.plan.chapter;
  renderAll();
  scheduleText();
  refreshStudyCounts();
}

function renderAll() { renderToday(); renderStudy(); renderAction(); renderBookSettings(); }

async function start() {
  bind();
  $("chapter").value = state.plan.chapter;
  $("paste").value = state.pasted;
  if (state.results) {                               // a reload during generation leaves nothing running
    for (const k of ["mcq", "cloze"]) if (state.results[k] && state.results[k].pending) state.results[k] = { error: "Interrupted before it finished." };
  }
  renderToday();
  try { state.config = await api("/api/config"); }
  catch (e) { showBanners(banner("error", `Couldn't load the app: ${e.message}`)); return; }
  if (storageMissing()) showBanners(storageBanner());
  if (state.config.passcode_required && !state.settings.passcode) openSettings("Enter the passcode to get started.");
  await loadServerState();
}

start();
