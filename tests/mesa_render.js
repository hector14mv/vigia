// Runs the Mesa page's script against a state, with just enough DOM to render it, and prints what each panel shows.
// Usage: node mesa_render.js <mesa.html> <state.json>
"use strict";
const fs = require("fs");
const [page, statePath] = process.argv.slice(2);
const scripts = [...fs.readFileSync(page, "utf8").matchAll(/<script>([\s\S]*?)<\/script>/g)];
const sample = fs.readFileSync(statePath, "utf8");
const els = {};
function el(id) {
  return els[id] || (els[id] = { id, innerHTML: "", textContent: id === "mesa-sample" ? sample : "", hidden: false,
    dataset: {}, setAttribute() {}, addEventListener() {} });
}
global.document = { getElementById: el, querySelectorAll: () => [] };
global.window = { matchMedia: () => ({ matches: false }) };
global.localStorage = { getItem: () => null, setItem() {} };
global.setInterval = () => {};
global.fetch = () => { throw new Error("the page must render the embedded state, not fetch"); };
new Function(scripts[scripts.length - 1][1])();
console.log(JSON.stringify({ turn: el("panel-turn").innerHTML, factory: el("panel-factory").innerHTML,
  feed: el("panel-feed").innerHTML, pulse: el("pulse").innerHTML }));
