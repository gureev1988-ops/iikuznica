export const byId = (id) => document.getElementById(id);

export function node(tag, className = "", ...children) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  element.append(...children);
  return element;
}

export function button(label, click, className = "button") {
  const element = node("button", className, label);
  element.type = "button";
  element.addEventListener("click", () => click(element));
  return element;
}

export function select(label, id, entries, value, change) {
  const input = node("select");
  input.id = id;
  for (const [key, text] of Object.entries(entries)) input.add(new Option(text, key));
  input.value = value;
  input.onchange = () => change(input.value);
  return node("label", "", label, input);
}

export function safeLink(label, url) {
  if (!url) return node("span", "", label);
  const target = new URL(url, location.href);
  if (!["https:", "http:"].includes(target.protocol)) return node("span", "", label);
  const link = node("a", "", label);
  link.href = target.href;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  return link;
}

const date = new Intl.DateTimeFormat("ru-RU", {
  timeZone: "Europe/Moscow", day: "2-digit", month: "2-digit", year: "numeric",
  hour: "2-digit", minute: "2-digit",
});
export function time(value) {
  const text = value ? date.format(new Date(value)) : "—";
  const element = node("time", "", text);
  if (value) element.dateTime = value;
  element.title = text + " МСК";
  return element;
}

export const empty = (text) => node("p", "desk-blank", text);
export const actions = (...controls) => node("div", "quality-actions", ...controls);
export const block = (title, value) => node("div", "desk-block",
  node("h4", "", title), node("p", "quality-copy", value));
