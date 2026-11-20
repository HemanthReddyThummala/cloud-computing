// A deliberately small DOM for the content-script tests (no dependencies). It implements only what
// content.js uses: getElementById, querySelector with descendant selectors of #id / .class / tag,
// createElement, prepend/append/remove, events, dataset, attributes. It is NOT a browser; the real
// YouTube page is checked manually (see the Phase C Part 3 session log).

class FakeElement {
  constructor(doc, tag) {
    this.ownerDocument = doc;
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.id = "";
    this.className = "";
    this.dataset = {};
    this.attributes = {};
    this.textContent = "";
    this.title = "";
    this.disabled = false;
    this.type = "";
    this.listeners = {};
  }

  setAttribute(name, value) { this.attributes[name] = String(value); }

  getAttribute(name) { return this.attributes[name] ?? null; }

  addEventListener(type, fn) { (this.listeners[type] ??= []).push(fn); }

  removeEventListener(type, fn) { this.listeners[type] = (this.listeners[type] ?? []).filter((f) => f !== fn); }

  prepend(child) {
    child.remove();
    child.parentNode = this;
    this.children.unshift(child);
  }

  append(...nodes) {
    for (const child of nodes) {
      child.remove();
      child.parentNode = this;
      this.children.push(child);
    }
  }

  remove() {
    if (!this.parentNode) return;
    const siblings = this.parentNode.children;
    siblings.splice(siblings.indexOf(this), 1);
    this.parentNode = null;
  }

  * descendants() {
    for (const child of this.children) {
      yield child;
      yield* child.descendants();
    }
  }

  matches(simple) {
    if (simple.startsWith("#")) return this.id === simple.slice(1);
    if (simple.startsWith(".")) return this.className.split(/\s+/).includes(simple.slice(1));
    return this.tagName === simple.toUpperCase();
  }

  querySelector(selector) { return query(this, selector); }

  querySelectorAll(selector) {
    const parts = selector.trim().split(/\s+/);
    return [...this.descendants()].filter((d) => d.matches(parts.at(-1)) && ancestorsMatch(d, parts.slice(0, -1), this));
  }

  /** Simulate a user click; resolves when all (async) listeners finished. */
  click() {
    if (this.disabled) return Promise.resolve();
    const event = { type: "click", currentTarget: this, preventDefault() {}, stopPropagation() {} };
    return Promise.all((this.listeners.click ?? []).map((fn) => fn(event)));
  }
}

function ancestorsMatch(node, parts, root) {
  let i = parts.length - 1;
  for (let n = node.parentNode; n && n !== root && i >= 0; n = n.parentNode) if (n.matches(parts[i])) i -= 1;
  return i < 0;
}

function query(root, selector) {
  const parts = selector.trim().split(/\s+/);
  for (const d of root.descendants()) {
    if (d.matches(parts.at(-1)) && ancestorsMatch(d, parts.slice(0, -1), root)) return d;
  }
  return null;
}

export class FakeDocument {
  constructor() {
    this.documentElement = new FakeElement(this, "html");
    this.body = new FakeElement(this, "body");
    this.documentElement.append(this.body);
    this.listeners = {};
  }

  createElement(tag) { return new FakeElement(this, tag); }

  getElementById(id) {
    for (const d of this.documentElement.descendants()) if (d.id === id) return d;
    return null;
  }

  querySelector(selector) { return query(this.documentElement, selector); }

  querySelectorAll(selector) { return this.documentElement.querySelectorAll(selector); }

  addEventListener(type, fn) { (this.listeners[type] ??= []).push(fn); }

  removeEventListener(type, fn) { this.listeners[type] = (this.listeners[type] ?? []).filter((f) => f !== fn); }

  dispatch(type) { for (const fn of this.listeners[type] ?? []) fn({ type }); }
}

export class FakeWindow {
  constructor(href) {
    this.location = { href };
    this.listeners = {};
  }

  addEventListener(type, fn) { (this.listeners[type] ??= []).push(fn); }

  removeEventListener(type, fn) { this.listeners[type] = (this.listeners[type] ?? []).filter((f) => f !== fn); }

  dispatch(type) { for (const fn of this.listeners[type] ?? []) fn({ type }); }
}

/** Build YouTube's player skeleton: #movie_player > .ytp-chrome-bottom > .ytp-right-controls. */
export function buildPlayer(doc) {
  const player = doc.createElement("div");
  player.id = "movie_player";
  const bottom = doc.createElement("div");
  bottom.className = "ytp-chrome-bottom";
  const right = doc.createElement("div");
  right.className = "ytp-right-controls";
  const settings = doc.createElement("button");
  settings.className = "ytp-button ytp-settings-button";
  right.append(settings);
  bottom.append(right);
  player.append(bottom);
  doc.body.append(player);
  return { player, toolbar: right };
}
