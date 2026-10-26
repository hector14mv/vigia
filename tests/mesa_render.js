// Runs the Mesa page's own scripts against a state under Node, with a small DOM, and prints what each view shows.
// Usage: node mesa_render.js <mesa.html> <state.json> <route>...
//
// The page builds its DOM with createElement and text nodes, so this is a DOM of that much: elements, text nodes,
// attributes, listeners, and the few document and window calls the page makes. Every route renders twice, at
// phone width and at desktop width, each in a fresh context with the state embedded as the page's sample (so the
// page never fetches). It prints {"phone": {route: view}, "desk": {route: view}}, where a view is the #app subtree
// as `html` (attributes kept, text escaped), as `text` (one line per block), and its `links` and `tags`. A route
// written `route!key` renders `route`, then clicks the element whose data-k is `key` once (a sheet opening, say).
// With `--live` first, it instead runs the page against a scripted server, as served (no embedded sample), and shows
// the route before and after one poll, with the delays of the timeouts the page asked for before it, during it, and
// after the timeouts it was then waiting on went off once:
// node mesa_render.js --live <mesa.html> <live.json> <route>, where live.json is
// {"state": [state, ...], "pr": [detail, ...]} and each fetch of that kind answers with the next entry (the last one
// repeats; an entry with "__status" answers with that HTTP error). It prints {"before": view, "after": view, "timers": {"before": [ms], "after": [ms], "retry": [ms]}} for the desktop layout.
// MESA_STORED (a JSON object in the environment) is what browser storage holds before the page loads (the saved
// tab, say); what the page saves comes back as `saved` in each view.
// Anything the page must never do (innerHTML, document.write, a fetch) throws, and so fails the test.
"use strict";
const fs = require("fs");
const vm = require("vm");

const live = process.argv[2] === "--live";
const [pagePath, statePath, ...routes] = process.argv.slice(live ? 3 : 2);
const scripts = [...fs.readFileSync(pagePath, "utf8").matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
const sample = live ? "" : fs.readFileSync(statePath, "utf8");
const BLOCK = new Set(["div", "p", "h1", "h2", "h3", "li", "ul", "ol", "article", "section", "header", "main", "aside",
  "nav", "pre", "tr", "th", "td", "fieldset", "legend", "label", "button", "textarea"]);

function refuse(what) {
  return () => { throw new Error(`the page used ${what}: it must build the DOM from text nodes`); };
}

class Text {
  constructor(data) { this.nodeType = 3; this.data = String(data); this.parentNode = null; }
  get textContent() { return this.data; }
}

class Element {
  constructor(tag, ns) {
    this.nodeType = 1;
    this.localName = String(tag).toLowerCase();
    this.tagName = this.localName.toUpperCase();
    this.namespaceURI = ns || "http://www.w3.org/1999/xhtml";
    this.attrs = new Map();
    this.childNodes = [];
    this.parentNode = null;
    this.listeners = {};
  }
  get firstChild() { return this.childNodes[0] || null; }
  appendChild(child) {
    if (!(child instanceof Element || child instanceof Text)) throw new TypeError("appendChild: not a node");
    if (child.parentNode) child.parentNode.removeChild(child);
    child.parentNode = this;
    this.childNodes.push(child);
    return child;
  }
  removeChild(child) {
    const i = this.childNodes.indexOf(child);
    if (i < 0) throw new Error("removeChild: not a child");
    this.childNodes.splice(i, 1);
    child.parentNode = null;
    return child;
  }
  setAttribute(k, v) { this.attrs.set(String(k), String(v)); }
  getAttribute(k) { return this.attrs.has(k) ? this.attrs.get(k) : null; }
  removeAttribute(k) { this.attrs.delete(k); }
  hasAttribute(k) { return this.attrs.has(k); }
  get className() { return this.getAttribute("class") || ""; }
  set className(v) { this.setAttribute("class", v); }
  get classList() {
    const el = this, names = () => el.className.split(/\s+/).filter(Boolean);
    return {
      contains: (n) => names().includes(n),
      add: (...n) => { el.className = [...new Set([...names(), ...n])].join(" "); },
      remove: (...n) => { el.className = names().filter((x) => !n.includes(x)).join(" "); },
      toggle: (n, force) => {
        const want = force === undefined ? !names().includes(n) : !!force;
        if (want) el.classList.add(n); else el.classList.remove(n);
        return want;
      },
    };
  }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  removeEventListener() {}
  contains(node) { for (; node; node = node.parentNode) if (node === this) return true; return false; }
  querySelector(sel) {
    const m = /^\[([\w-]+)="(.*)"\]$/.exec(sel);
    if (!m) throw new Error("querySelector: the harness only knows [attr=\"value\"], not " + sel);
    return walk(this).find((el) => el !== this && el.getAttribute(m[1]) === m[2]) || null;
  }
  focus() { this.ownerDocument.activeElement = this; }
  get textContent() { return this.childNodes.map((c) => c.textContent).join(""); }
}
for (const name of ["innerHTML", "outerHTML"]) {
  Object.defineProperty(Element.prototype, name, { get: refuse(name), set: refuse(name) });
}
Element.prototype.insertAdjacentHTML = refuse("insertAdjacentHTML");

function walk(node, out = []) {
  if (node instanceof Element) { out.push(node); node.childNodes.forEach((c) => walk(c, out)); }
  return out;
}

function makeDocument() {
  const doc = { activeElement: null, listeners: {} };
  const make = (tag, ns) => Object.assign(new Element(tag, ns), { ownerDocument: doc });
  doc.documentElement = make("html");
  doc.body = make("body");
  const app = make("div");
  app.setAttribute("id", "app");
  doc.body.appendChild(app);
  const embedded = make("script");
  embedded.childNodes.push(new Text(sample));
  doc.createElement = (tag) => make(tag);
  doc.createElementNS = (ns, tag) => make(tag, ns);
  doc.createTextNode = (s) => new Text(s);
  doc.getElementById = (id) => ({ app, "mesa-sample": embedded })[id] || null;
  doc.addEventListener = (type, fn) => { (doc.listeners[type] = doc.listeners[type] || []).push(fn); };
  doc.write = refuse("document.write");
  return { doc, app };
}

function serialize(node) {
  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  if (node instanceof Text) return esc(node.data);
  const attrs = [...node.attrs].map(([k, v]) => ` ${k}="${esc(v).replace(/"/g, "&quot;")}"`).join("");
  return `<${node.localName}${attrs}>${node.childNodes.map(serialize).join("")}</${node.localName}>`;
}

function lines(node) {
  const parts = [];
  (function visit(n) {
    if (n instanceof Text) { parts.push(n.data); return; }
    const block = BLOCK.has(n.localName);
    if (block) parts.push("\n");
    n.childNodes.forEach(visit);
    if (block) parts.push("\n");
  })(node);
  return parts.join("").split("\n").map((l) => l.replace(/\s+/g, " ").trim()).filter(Boolean).join("\n");
}

function render(desk, route) {
  const [path, click] = route.split("!");
  const { doc, app } = makeDocument();
  const saved = JSON.parse(process.env.MESA_STORED || "{}");
  const win = {
    document: doc,
    location: { hash: "#/" + path },
    navigator: {},
    isSecureContext: false,
    localStorage: { getItem: (k) => (k in saved ? saved[k] : null), setItem: (k, v) => { saved[k] = String(v); }, removeItem: (k) => { delete saved[k]; } },
    matchMedia: (q) => ({ matches: desk && /min-width:\s*1024px/.test(q), addEventListener() {}, addListener() {} }),
    addEventListener() {},
    scrollTo() {},
    setInterval: () => 0,
    setTimeout: () => 0,
    clearTimeout() {},
    fetch: () => { throw new Error("the page must render the embedded state, not fetch"); },
    console,
  };
  win.window = win;
  const context = vm.createContext(win);
  scripts.forEach((code, i) => vm.runInContext(code, context, { filename: `mesa.html <script> #${i + 1}` }));
  if (click) {
    const target = walk(app).find((el) => el.getAttribute("data-k") === click);
    if (!target) throw new Error(`nothing to click: no data-k="${click}" on ${path}`);
    const event = { target, preventDefault() {}, stopPropagation() {} };
    (target.listeners.click || []).forEach((fn) => fn(event));
  }
  const els = walk(app);
  return {
    html: serialize(app),
    text: lines(app),
    links: els.filter((el) => el.hasAttribute("href")).map((el) => el.getAttribute("href")),
    tags: [...new Set(els.map((el) => el.localName))].sort(),
    saved,
  };
}

function view(app) {
  const els = walk(app);
  return { html: serialize(app), text: lines(app), links: els.filter((el) => el.hasAttribute("href")).map((el) => el.getAttribute("href")) };
}

const settle = async () => { for (let i = 0; i < 10; i++) await new Promise((r) => setImmediate(r)); };

async function renderLive(route) {
  const script = JSON.parse(fs.readFileSync(statePath, "utf8"));
  const { doc, app } = makeDocument();
  const seen = { state: 0, pr: 0 };
  const answer = (kind, url) => {
    const list = script[kind];
    const body = list[Math.min(seen[kind]++, list.length - 1)];
    if (body && body.__hang) return new Promise(() => {}); // an entry {"__hang": true} never answers
    const status = body && body.__status; // an entry {"__status": 502, ...} answers with that HTTP error
    return Promise.resolve({ ok: !status, status: status || 200, json: () => Promise.resolve(body), url });
  };
  let poll = null;
  const timers = [], waiting = []; // the delay of each setTimeout the page asks for, in ms, in order; and their callbacks
  const win = {
    document: doc,
    location: { hash: "#/" + route },
    navigator: {},
    isSecureContext: false,
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    matchMedia: (q) => ({ matches: /min-width:\s*1024px/.test(q), addEventListener() {}, addListener() {} }),
    addEventListener() {},
    scrollTo() {},
    setInterval: (fn) => { poll = fn; return 0; },
    setTimeout: (fn, ms) => { timers.push(ms); waiting.push(fn); return timers.length; },
    clearTimeout() {},
    fetch: (url) => answer(String(url).startsWith("/api/pr/") ? "pr" : "state", url),
    console,
  };
  win.window = win;
  const context = vm.createContext(win);
  scripts.forEach((code, i) => vm.runInContext(code, context, { filename: `mesa.html <script> #${i + 1}` }));
  await settle();
  const before = view(app);
  const scheduled = timers.length;
  if (!poll) throw new Error("the page did not start a poll");
  poll();
  await settle();
  const polled = timers.length;
  waiting.splice(0).forEach((fn) => fn()); // the timers the page is waiting on go off, once
  await settle();
  const timeouts = { before: timers.slice(0, scheduled), after: timers.slice(scheduled, polled), retry: timers.slice(polled) };
  return { before, after: view(app), timers: timeouts };
}

if (live) {
  renderLive(routes[0]).then((r) => process.stdout.write(JSON.stringify(r)), (e) => { console.error(e); process.exit(1); });
} else {
  const out = { phone: {}, desk: {} };
  for (const route of routes) {
    out.phone[route] = render(false, route);
    out.desk[route] = render(true, route);
  }
  process.stdout.write(JSON.stringify(out));
}
