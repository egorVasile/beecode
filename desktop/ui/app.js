"use strict";

/* global marked, DOMPurify */

const $ = (id) => document.getElementById(id);
const chat = $("chat"), input = $("input"), sessionsBox = $("sessions");

let sessionId = null;
let model = null;
let models = [];
let modelsProvider = "";
let selectedEffort = "";
let permMode = "ask";
let workdir = "";
let streaming = false;
let streamAbort = null;
let attached = [];
let titles = {};

function fatal(text) {
  document.getElementById("app").style.display = "none";
  const box = $("fatal");
  box.classList.remove("hidden");
  box.textContent = text;
}

async function api(path, opts) {
  const response = await fetch(path, opts);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(body.error || body.output || ("HTTP " + response.status));
    error.body = body;
    error.status = response.status;
    throw error;
  }
  return body;
}
const post = (path, data) => api(path, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(data || {}),
});

const chatHistory = [];
let chatHistoryIndex = -1;
let navigatingChat = false;
function updateNavigation() {
  $("nav-back").disabled = streaming || navigatingChat || chatHistoryIndex <= 0;
  $("nav-forward").disabled = streaming || navigatingChat || chatHistoryIndex < 0 || chatHistoryIndex >= chatHistory.length - 1;
  $("nav-sidebar").setAttribute("aria-pressed", String(!$("sidebar").classList.contains("menu-collapsed")));
}
function rememberChat(id) {
  if (!id || chatHistory[chatHistoryIndex] === id) return;
  chatHistory.splice(chatHistoryIndex + 1);
  chatHistory.push(id);
  if (chatHistory.length > 100) chatHistory.shift();
  chatHistoryIndex = chatHistory.length - 1;
  updateNavigation();
}
async function navigateChat(step) {
  const target = chatHistoryIndex + step;
  if (streaming || navigatingChat || target < 0 || target >= chatHistory.length) return;
  navigatingChat = true;
  updateNavigation();
  try {
    if (await switchSession(chatHistory[target], false)) chatHistoryIndex = target;
  } finally { navigatingChat = false; updateNavigation(); }
}
$("nav-back").onclick = () => navigateChat(-1);
$("nav-forward").onclick = () => navigateChat(1);
$("nav-sidebar").onclick = () => runMenuAction("sidebar");

// Keep the editor selection when a menu item is clicked.
let menuEditor = null;
document.addEventListener("focusin", (event) => {
  if (event.target.matches("input, textarea, [contenteditable=true]")) menuEditor = event.target;
});
document.querySelectorAll(".app-menu-group").forEach((group) => {
  group.addEventListener("toggle", () => {
    if (group.open) document.querySelectorAll(".app-menu-group").forEach((other) => { if (other !== group) other.open = false; });
  });
});
document.addEventListener("click", (event) => {
  if (!event.target.closest("#app-menu")) document.querySelectorAll(".app-menu-group").forEach((group) => { group.open = false; });
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") document.querySelectorAll(".app-menu-group").forEach((group) => { group.open = false; });
});
function setSidebarCollapsed(collapsed) {
  const sidebar = $("sidebar");
  sidebar.classList.toggle("menu-collapsed", collapsed);
  $("sidebar-wrap").classList.toggle("menu-collapsed", collapsed);
  sidebar.inert = collapsed;
  sidebar.setAttribute("aria-hidden", String(collapsed));
  updateNavigation();
}
function runMenuAction(action) {
  document.querySelectorAll(".app-menu-group").forEach((group) => { group.open = false; });
  if (action === "new-chat") newChat();
  else if (action === "settings") openSettings();
  else if (action === "instructions") { openSettings(); document.querySelector('[data-tab="instructions"]').click(); }
  else if (action === "sidebar") setSidebarCollapsed(!$("sidebar").classList.contains("menu-collapsed"));
  else if (action === "search") { setSidebarCollapsed(false); $("search").focus(); }
  else {
    if (["undo", "redo", "cut", "copy", "paste", "selectAll"].includes(action) && menuEditor?.isConnected) menuEditor.focus();
    if (window.beeDesk?.menuAction) window.beeDesk.menuAction(action);
  }
}
document.querySelectorAll("[data-menu-action]").forEach((button) => {
  button.addEventListener("click", () => runMenuAction(button.dataset.menuAction));
});
document.addEventListener("keydown", (event) => {
  let action = "";
  if (event.key === "F11" && !event.ctrlKey && !event.altKey) action = "fullscreen";
  else if (event.ctrlKey && !event.altKey && !event.metaKey) {
    if (event.shiftKey && event.code === "KeyS") action = "sidebar";
    else if (!event.shiftKey && event.code === "KeyN") action = "new-chat";
    else if (!event.shiftKey && event.code === "KeyF") action = "search";
    else if (!event.shiftKey && event.code === "Comma") action = "settings";
    else if (!event.shiftKey && event.code === "KeyW") action = "close";
    else if (event.code === "Equal" || event.code === "NumpadAdd") action = "zoomIn";
    else if (event.code === "Minus" || event.code === "NumpadSubtract") action = "zoomOut";
    else if (event.code === "Digit0" || event.code === "Numpad0") action = "zoomReset";
  }
  if (action) { event.preventDefault(); runMenuAction(action); }
});

let followChat = true;
function scrollDown(force = false) {
  if (!force && !followChat) return;
  chat.scrollTop = chat.scrollHeight;
  followChat = true;
}
chat.addEventListener("scroll", () => {
  followChat = chat.scrollHeight - chat.scrollTop - chat.clientHeight < 80;
});

/* Message landmarks in the current conversation. */
const chatMap = $("chat-map"), chatMapPreview = $("chat-map-preview");
let chatMapRows = [], chatMapFrame = 0;
function showChatMapPreview(index, button) {
  const row = chatMapRows[index];
  if (!row) return;
  let sibling = row.nextElementSibling, answer = "";
  while (sibling && !sibling.classList.contains("user")) {
    if (sibling.classList.contains("assistant")) {
      const text = (sibling.innerText || sibling.textContent || "").replace(/\s+/g, " ").trim();
      if (text && text !== "Ожидание ответа") { answer = text; break; }
    }
    sibling = sibling.nextElementSibling;
  }
  chatMapPreview.querySelector(".chat-map-title").textContent = (row.textContent || "").replace(/\s+/g, " ").trim();
  chatMapPreview.querySelector(".chat-map-answer").textContent = answer || "Пока нет ответа";
  chatMapPreview.hidden = false;
  const area = $("chat-area"), preferred = chatMap.offsetTop + button.offsetTop - chatMap.scrollTop - 12;
  chatMapPreview.style.top = Math.max(4, Math.min(preferred, area.clientHeight - chatMapPreview.offsetHeight - 4)) + "px";
}
function hideChatMapPreview() { chatMapPreview.hidden = true; }
function updateChatMapActive() {
  if (!chatMapRows.length) return;
  const top = chat.getBoundingClientRect().top + 40;
  let active = 0;
  chatMapRows.forEach((row, i) => { if (row.getBoundingClientRect().top <= top) active = i; });
  if (chat.scrollHeight - chat.scrollTop - chat.clientHeight < 4) active = chatMapRows.length - 1;
  [...chatMap.children].forEach((button, i) => {
    button.classList.toggle("active", i === active);
    if (i === active) button.setAttribute("aria-current", "location");
    else button.removeAttribute("aria-current");
  });
}
function rebuildChatMap() {
  chatMapFrame = 0;
  chatMapRows = [...chat.children].filter(node => node.classList.contains("msg") && node.classList.contains("user"));
  chatMap.replaceChildren();
  chatMap.hidden = chatMapRows.length === 0;
  hideChatMapPreview();
  chatMapRows.forEach((row, index) => {
    const button = document.createElement("button");
    button.type = "button"; button.className = "chat-map-tick";
    const label = (row.textContent || "").replace(/\s+/g, " ").trim();
    button.setAttribute("aria-label", `Сообщение ${index + 1}: ${label.slice(0, 100)}`);
    button.addEventListener("mouseenter", () => showChatMapPreview(index, button));
    button.addEventListener("focus", () => showChatMapPreview(index, button));
    button.addEventListener("blur", hideChatMapPreview);
    button.addEventListener("click", () => {
      followChat = false;
      const top = chat.scrollTop + row.getBoundingClientRect().top - chat.getBoundingClientRect().top - 16;
      chat.scrollTo({top: Math.max(0, top), behavior: reduceMotion() ? "auto" : "smooth"});
      hideChatMapPreview();
    });
    chatMap.append(button);
  });
  updateChatMapActive();
}
chatMap.addEventListener("mouseleave", hideChatMapPreview);
chat.addEventListener("scroll", updateChatMapActive, {passive: true});
new MutationObserver(() => {
  if (!chatMapFrame) chatMapFrame = requestAnimationFrame(rebuildChatMap);
}).observe(chat, {childList: true});

/* ---------- greeting & identity ---------- */

function greet() {
  const hour = new Date().getHours();
  let head, sub;
  if (hour >= 5 && hour < 12) {
    head = "Доброе утро.";
    sub = "Кофе — тебе, задачи — мне.";
  } else if (hour >= 12 && hour < 18) {
    head = "Добрый день.";
    sub = "Что делаем?";
  } else if (hour >= 18 && hour < 23) {
    head = "Добрый вечер.";
    sub = "День был длинным — давай закроем хвосты.";
  } else {
    head = "Поздно. Береги себя.";
    sub = "Я не сплю — спрашивай.";
  }
  $("greet-text").textContent = head;
  $("greet-sub").textContent = sub;
  scrambleText($("greet-text"), head);
}

async function identify() {
  try {
    const body = await api("/api/whoami");
    const name = (body.name || "").trim() || "друг";
    $("whoami").textContent = name;
    $("avatar").textContent = name[0].toUpperCase();
  } catch {
    $("whoami").textContent = "друг";
    $("avatar").textContent = "·";
  }
  try {
    const version = await api("/api/health");
    $("ver").textContent = "v" + (version.version || "");
  } catch { /* badge stays empty */ }
}

/* ---------- markdown ---------- */

/* ---------- model slash-markup (/b/ /i/ /u/ /c.color/) ---------- */
// Same four tags the terminal paints (beeagent/core/markup.py): the model is
// taught them by the system prompt, so the desktop must honor them too instead
// of showing raw "/b read/". Bold/italic ride the normal Markdown path,
// underline/highlight have no Markdown spelling and travel as plain tags that
// DOMPurify keeps.

const HL_COLORS = ["red", "green", "yellow", "blue", "magenta", "cyan", "white", "black",
  "orange", "purple", "violet", "pink", "teal", "gray", "grey", "brown", "lime"];

function isWordChar(ch) {
  return !!ch && /[A-Za-z0-9_]/.test(ch);
}

function findCloser(text, start) {
  const n = text.length;
  let j = start;
  while (j < n) {
    if (text[j] === "\\" && j + 1 < n && text[j + 1] === "/") { j += 2; continue; }
    if (text[j] === "/") {
      const nxt = j + 1 < n ? text[j + 1] : "";
      if (!nxt || !isWordChar(nxt)) return j;
    }
    j += 1;
  }
  return null;
}

function closingTag(text, start, closer) {
  // Body up to the next bare "/" — or to the end of the line when unclosed.
  const n = text.length;
  let j = start;
  while (j < n) {
    if (text[j] === "\\" && j + 1 < n && text[j + 1] === "/") { j += 2; continue; }
    if (text[j] === "/") {
      const nxt = j + 1 < n ? text[j + 1] : "";
      if (nxt && isWordChar(nxt)) { j += 1; continue; }
      const body = text.slice(start, j);
      if (!body.trim()) return null;
      return [closer(body), j + 1];
    }
    if (text[j] === "\n") {
      const body = text.slice(start, j);
      if (!body.trim()) return null;
      return [closer(body), j];
    }
    j += 1;
  }
  const body = text.slice(start);
  if (!body.trim()) return null;
  return [closer(body), n];
}

function mdClose(low) {
  if (low === "b") return (body) => `**${body}**`;
  if (low === "i") return (body) => `*${body}*`;
  return (body) => `<u>${body}</u>`;
}

function tryTag(text, i) {
  const n = text.length;
  if (i + 1 >= n) return null;
  const letter = text[i + 1];
  const low = letter.toLowerCase();
  const rest = i + 2;
  if (low === "b" || low === "i" || low === "u") {
    const explicit = /^\/([biu])([^\n]+?)\/\1(?![\p{L}\p{N}_])/iu.exec(text.slice(i));
    if (explicit && explicit[2].trim()) return [mdClose(low)(explicit[2].trim()), i + explicit[0].length];
    if (rest < n && (text[rest] === " " || text[rest] === "\t" || text[rest] === "\n")) {
      return closingTag(text, rest + 1, mdClose(low));
    }
    if (rest < n && isWordChar(text[rest]) && findCloser(text, rest) !== null) {
      return closingTag(text, rest, mdClose(low));
    }
    return null;
  }
  if (letter === "с" || letter === "c") {
    if (rest >= n || text[rest] !== ".") return null;
    // Some models emit /с.redТекст/с. instead of the documented slash syntax.
    // Only repair it when an explicit closing marker makes the intent clear.
    const explicit = /^(red|green|yellow|blue|magenta|cyan|white|black|orange|purple|violet|pink|teal|gray|grey|brown|lime)([^\n]+?)\/[cс]\./i.exec(text.slice(rest + 1));
    if (explicit && explicit[2].trim()) {
      return [`<mark class="hl-${explicit[1].toLowerCase()}">${explicit[2].trimStart()}</mark>`, rest + 1 + explicit[0].length];
    }
    const match = /^([A-Za-z#][A-Za-z0-9#]*)\s/.exec(text.slice(rest + 1));
    if (!match) return null;
    const color = match[1].toLowerCase();
    const ok = HL_COLORS.includes(color) || /^#[0-9a-fA-F]{3}([0-9a-fA-F]{3})?$/.test(match[1]);
    if (!ok) return null;
    const cls = HL_COLORS.includes(color) ? ` class="hl-${color}"` : "";
    return closingTag(text, rest + 1 + match[0].length, (body) => `<mark${cls}>${body}</mark>`);
  }
  return null;
}

function stylizeChunk(chunk) {
  let out = "";
  let i = 0;
  const n = chunk.length;
  while (i < n) {
    const ch = chunk[i];
    // Models sometimes reuse the one-time title delimiters for a heading.
    // Repair standalone headings in prose, leaving fenced and inline code alone.
    if (ch === "█" && (i === 0 || chunk[i - 1] === "\n")) {
      const heading = /^█([^█\n]{1,80})█(?=\s*(?:\n|$))/.exec(chunk.slice(i));
      if (heading && heading[1].trim()) {
        out += `**${heading[1].trim()}**`;
        i += heading[0].length;
        continue;
      }
    }
    if (ch === "." && (i === 0 || chunk[i - 1] === "\n")) {
      const malformed = /^\.mode([^\n/]{1,80})\/:(?=\s*(?:\n|$))/.exec(chunk.slice(i));
      if (malformed && malformed[1].trim()) {
        out += `**${malformed[1].trim()}:**`;
        i += malformed[0].length;
        continue;
      }
    }
    if (ch === "\\" && i + 1 < n && (chunk[i + 1] === "`" || chunk[i + 1] === "/")) {
      out += chunk[i + 1];
      i += 2;
      continue;
    }
    if (ch === "`") {
      const end = chunk.indexOf("`", i + 1);
      if (end === -1) { out += chunk.slice(i); break; }
      out += chunk.slice(i, end + 1);
      i = end + 1;
      continue;
    }
    if (ch === "/" && (i === 0 || !isWordChar(chunk[i - 1]))) {
      const replaced = tryTag(chunk, i);
      if (replaced !== null) {
        out += replaced[0];
        i = replaced[1];
        continue;
      }
    }
    out += ch;
    i += 1;
  }
  return out;
}

function stylizeProse(text) {
  // Fenced blocks pass through untouched; everything else is scanned.
  const parts = String(text || "").split(/(```.*?```|~~~.*?~~~)/s);
  return parts.map((p, i) => (i % 2 === 0 ? stylizeChunk(p) : p)).join("");
}

function renderMarkdown(text) {
  const box = document.createElement("div");
  box.className = "md";
  box.innerHTML = DOMPurify.sanitize(marked.parse(stylizeProse(text || ""), { breaks: true }));
  for (const pre of box.querySelectorAll("pre")) {
    if (foldToolCall(pre, box)) continue;
    const code = pre.querySelector("code");
    const lang = (code && code.className.replace("language-", "")) || "code";
    const head = document.createElement("div");
    head.className = "codehead";
    const label = document.createElement("span");
    label.textContent = lang;
    const copy = document.createElement("button");
    copy.textContent = "Копировать";
    copy.onclick = async () => {
      try {
        await navigator.clipboard.writeText(code ? code.textContent : pre.textContent);
        copy.textContent = "Скопировано";
      } catch {
        copy.textContent = "Не удалось";
      }
      setTimeout(() => { copy.textContent = "Копировать"; }, 1600);
    };
    head.append(label, copy);
    const block = document.createElement("div");
    block.className = "code-block";
    pre.before(block);
    block.append(head, pre);
  }
  return box;
}

/* Raw {"tool": ...} JSON blocks are wire protocol, not content: the executed
   call already shows as a tidy card. Fold them into a compact chip instead of
   a wall of dark JSON. Anything that is not exactly one tool call stays code. */
function foldToolCall(pre, box) {
  const code = pre.querySelector("code");
  const raw = (code ? code.textContent : pre.textContent || "").trim();
  if (!raw.startsWith("{") || !raw.endsWith("}")) return false;
  let call;
  try {
    call = JSON.parse(raw);
  } catch {
    return false;
  }
  if (!call || typeof call !== "object" || typeof call.tool !== "string") return false;
  const head = pre.previousElementSibling;
  if (head && head.classList && head.classList.contains("codehead")) head.remove();
  const details = document.createElement("details");
  details.className = "toolcall";
  const summary = document.createElement("summary");
  summary.append(dotSpan("run"));
  const label = document.createElement("span");
  const args = (call.args && typeof call.args === "object") ? call.args : {};
  if (call.tool === "think" && typeof args.plan === "string" && args.plan.trim()) {
    label.textContent = "💭 " + args.plan.replace(/\s+/g, " ").slice(0, 90);
  } else {
    label.textContent = "⚙ " + call.tool + argsHint(args);
  }
  summary.append(label);
  details.append(summary);
  const out = document.createElement("pre");
  out.textContent = slimArgs(args);
  details.append(out);
  pre.replaceWith(details);
  return true;
}

function argsHint(args) {
  for (const key of ["path", "command", "pattern", "old_text", "url", "model"]) {
    if (typeof args[key] === "string" && args[key]) {
      return " · " + key + "=" + args[key].slice(0, 60);
    }
  }
  return "";
}

function slimArgs(args) {
  const cut = (value) => {
    if (typeof value === "string" && value.length > 300) {
      return value.slice(0, 300) + `…(${value.length} символов)`;
    }
    return value;
  };
  const walk = (value) => {
    if (typeof value === "string") return cut(value);
    if (Array.isArray(value)) return value.map(walk);
    if (value && typeof value === "object") {
      const out = {};
      for (const [key, val] of Object.entries(value)) out[key] = walk(val);
      return out;
    }
    return value;
  };
  return JSON.stringify(walk(args), null, 1).slice(0, 1500);
}

/* The model sometimes echoes the transcript it was shown ("User: [tool
   result]..." roleplay). Those lines are not its words — render them as the
   same tidy cards live turns get, and drop a lone "Assistant:" label. */
const ECHO_HEAD = /^\s*(?:User|user)\s*:\s*\[tool result\]\s*(?:tool=([A-Za-z0-9_]+)\s+error=(True|False))?\s*$/;
const ECHO_RESULT = /^\[tool result\]\s*tool=([A-Za-z0-9_]+)\s+error=(True|False)\s*$/;
const ROLE_LABEL = /^(?:Assistant|Ассистент)\s*:\s*/;

function takeEchoOutput(lines, j, tool, failed) {
  while (j < lines.length && (!lines[j].trim() || lines[j].trim() === "<bee-data>")) j += 1;
  if (!tool && j < lines.length) {
    const inner = ECHO_RESULT.exec(lines[j].trim());
    if (inner) {
      tool = inner[1];
      failed = inner[2] === "True";
      j += 1;
    }
  }
  if (!tool) return null;
  const out = [];
  for (; j < lines.length && out.length < 60; j += 1) {
    const trimmed = lines[j].trim();
    if (trimmed === "</bee-data>") {
      j += 1;
      break;
    }
    if (/^\s*(?:User|user|Assistant|Ассистент)\s*:/.test(lines[j]) || lines[j].startsWith("```")) break;
    out.push(lines[j]);
  }
  return { tool, failed, output: out.join("\n").trim().slice(0, 1500) || "(пусто)", next: j };
}

function splitEchoes(text) {
  const parts = [];
  const chunks = String(text || "").split(/(```.*?```|~~~.*?~~~)/s);
  chunks.forEach((chunk, index) => {
    if (index % 2 === 1 || !chunk) {
      if (chunk) parts.push({ t: "md", text: chunk });
      return;
    }
    let prose = [];
    const flushProse = () => {
      if (prose.length) {
        parts.push({ t: "md", text: prose.join("\n") });
        prose = [];
      }
    };
    const lines = chunk.split("\n");
    for (let i = 0; i < lines.length; i += 1) {
      const trimmed = lines[i].trim();
      if (trimmed === "<bee-data>" || trimmed === "</bee-data>") continue;
      const head = ECHO_HEAD.exec(lines[i]);
      if (!head) {
        const stripped = lines[i].replace(ROLE_LABEL, "");
        if (stripped !== lines[i] && !stripped.trim()) continue; // bare label line
        prose.push(stripped);
        continue;
      }
      const echo = takeEchoOutput(lines, i + 1,
        head[1] || "", head[2] === "True");
      if (!echo) {
        prose.push(lines[i]);
        continue;
      }
      i = echo.next - 1;
      flushProse();
      parts.push({ t: "tool", tool: echo.tool, error: echo.failed, output: echo.output });
    }
    flushProse();
  });
  return parts;
}

function animateToolDisclosure(details, summary) {
  let animation = null;
  let targetOpen = false;
  summary.addEventListener("click", (event) => {
    if (reduceMotion() || typeof details.animate !== "function") return;
    event.preventDefault();
    const start = details.getBoundingClientRect().height;
    const next = animation ? !targetOpen : !details.open;
    if (animation) { animation.cancel(); animation = null; }
    targetOpen = next;
    details.style.overflow = "hidden";
    details.open = true;
    const end = next ? details.scrollHeight : summary.getBoundingClientRect().height;
    animation = details.animate([{height: `${start}px`}, {height: `${end}px`}], {
      duration: 240, easing: "cubic-bezier(.22, 1, .36, 1)"
    });
    const running = animation;
    running.onfinish = () => {
      if (animation !== running) return;
      animation = null;
      details.open = targetOpen;
      details.style.overflow = "";
    };
    running.oncancel = () => { if (animation === running) animation = null; };
  });
}

function animateActivityDisclosure(details, summary, body) {
  let animation = null, targetOpen = false;
  summary.addEventListener('click', event => {
    if (reduceMotion() || typeof body.animate !== 'function') return;
    event.preventDefault();
    const start = details.open ? body.getBoundingClientRect().height : 0;
    const next = animation ? !targetOpen : !details.open;
    if (animation) { animation.cancel(); animation = null; }
    targetOpen = next;
    body.style.height = `${start}px`;
    details.open = true;
    const end = next ? body.scrollHeight + (body.offsetHeight - body.clientHeight) : 0;
    animation = body.animate([{height: `${start}px`, opacity: start ? 1 : 0},
      {height: `${end}px`, opacity: next ? 1 : 0}],
      {duration: 300, easing: 'cubic-bezier(.2, .75, .25, 1)', fill: 'both'});
    const running = animation;
    running.onfinish = () => {
      if (animation !== running) return;
      animation = null;
      details.open = targetOpen;
      body.style.height = '';
      running.cancel();
    };
  });
}

function echoToolCard(tool, error, output) {
  const details = document.createElement("details");
  details.className = "tool";
  const summary = document.createElement("summary");
  summary.append(dotSpan(error ? "fail" : "ok"));
  const label = document.createElement("span");
  label.textContent = `${tool || "tool"} — ${error ? "ошибка" : "готово"}`;
  summary.append(label);
  details.append(summary);
  animateToolDisclosure(details, summary);
  const pre = document.createElement("pre");
  pre.textContent = output;
  details.append(pre);
  if (error) details.open = true;
  return details;
}

function renderAssistant(text) {
  const wrap = document.createElement("div");
  wrap.className = "assistant-body";
  const clean = String(text || "").replace(/^\s*(?:Assistant|Ассистент)\s*:\s*/, "");
  for (const part of splitEchoes(clean)) {
    if (part.t === "tool") {
      wrap.append(echoToolCard(part.tool, part.error, part.output));
    } else if (part.text.trim()) {
      wrap.append(renderMarkdown(part.text));
    }
  }
  if (!wrap.hasChildNodes()) wrap.append(renderMarkdown(text || ""));
  return wrap;
}

/* ---------- rows ---------- */

function userRow(text) {
  currentToolGroup = null;
  pendingActivityLabel = "";
  const div = document.createElement("div");
  div.className = "msg user";
  div.textContent = text;
  chat.append(div);
  scrollDown(true);
  popIn(div);
  return div;
}

function assistantBox() {
  const div = document.createElement("div");
  div.className = "msg assistant";
  const stream = document.createElement("div");
  stream.className = "stream md";
  stream.innerHTML = '<span class="thinking-indicator" role="status" aria-label="Ожидание ответа"><span>Ожидание ответа</span><span class="typing" aria-hidden="true"><i></i><i></i><i></i></span></span>';
  div.append(stream);
  chat.append(div);
  scrollDown();
  stampIn(div);
  return { div, stream, text: "" };
}

function ensureTurnBox(turn) {
  if (!turn.box) {
    turn.rawToolHidden = false;
    turn.draftLive = null;
    turn.draftKey = null;
    turn.box = assistantBox();
  }
  return turn.box;
}

function renderStreaming(box) {
  if (box.renderScheduled) return;
  box.renderScheduled = true;
  requestAnimationFrame(() => {
    box.renderScheduled = false;
    if (!box.stream.isConnected || box.stream.style.display === "none") return;
    box.stream.innerHTML = DOMPurify.sanitize(marked.parse(stylizeProse(box.text), { breaks: true }));
    scrollDown();
  });
}

function dotSpan(kind) {
  const dot = document.createElement("span");
  dot.className = "dot " + kind;
  return dot;
}

// One visual block per tool call: a "start" opens it, the matching "end"
// updates the SAME block (dot, label, output) instead of appending a second
// one. Calls are matched by the backend's `call` id; events without one
// (old servers, transcript replay) fall back to the tool name.
const liveTools = {};
let currentToolGroup = null, pendingActivityLabel = "";

// A whole-line status is metadata, not part of the assistant's answer.
// Re-scan each streamed segment so a marker split across network chunks stays hidden.
function extractActivity(text, final = false) {
  const lines = String(text || '').split('\n');
  let fence = '', clean = '', label = '', sequence = 0;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i], last = i === lines.length - 1;
    const marker = /^\s*(`{3,}|~{3,})/.exec(line);
    if (marker) {
      if (!fence) fence = marker[1][0]; else if (fence === marker[1][0]) fence = '';
    }
    const status = !fence && !marker && /^\s*⟦действие:\s*([^⟦⟧\r\n]{1,120}?)\s*⟧\s*$/i.exec(line);
    if (status) { label = status[1].trim(); sequence = i + 1; continue; }
    const prefix = line.trimStart();
    if (!fence && last && !final && prefix && ('⟦действие:'.startsWith(prefix.toLowerCase()) ||
        (prefix.toLowerCase().startsWith('⟦действие:') && !prefix.includes('⟧') && prefix.length <= 140))) continue;
    clean += line + (last ? '' : '\n');
  }
  return {text: clean, label, sequence};
}

function applyActivityLabel(label) {
  if (!label) return;
  pendingActivityLabel = label;
  if (currentToolGroup && currentToolGroup.isConnected) {
    currentToolGroup.querySelector('.activity-label').textContent = label;
    const last = currentToolGroup.querySelector('.tool:last-child');
    if (last) last.dataset.activity = label;
  }
}

function activityText(tool, state) {
  const running = {read:'Читает файл', write:'Записывает файл', edit:'Редактирует файл', patch:'Изменяет файлы',
    bash:'Выполняет команду', shell:'Выполняет команду', list_directory:'Просматривает папку',
    grep:'Ищет в файлах', glob:'Ищет файлы', think:'Планирует работу', web_search:'Ищет в интернете',
    web_fetch:'Открывает страницу', subagent:'Запускает субагента'};
  const done = {read:'Прочитал файл', write:'Записал файл', edit:'Отредактировал файл', patch:'Изменил файлы',
    bash:'Выполнил команду', shell:'Выполнил команду', list_directory:'Просмотрел папку',
    grep:'Нашёл в файлах', glob:'Нашёл файлы', think:'Составил план', web_search:'Выполнил поиск',
    web_fetch:'Открыл страницу', subagent:'Запустил субагента'};
  if (state === 'fail') return 'Не удалось: ' + (toolTitle(tool) || 'инструмент').toLowerCase();
  return (state === 'run' ? running : done)[tool] || (state === 'run' ? 'Работает: ' : 'Выполнил: ') + toolTitle(tool);
}

function updateActivity(details, tool, state) {
  const group = details.closest('.tool-group');
  if (!group) return;
  const custom = details.dataset.activity || '';
  group.querySelector('.activity-label').textContent = state === 'fail'
    ? (custom ? 'Ошибка: ' + custom : activityText(tool, state)) : (custom || activityText(tool, state));
  group.querySelector('.activity-dot').className = 'activity-dot ' + state;
  const count = group.querySelectorAll('.tool').length;
  group.querySelector('.activity-count').textContent = String(count);
}

function toolKey(data) {
  if (data && data.call != null) return "call:" + data.call;
  return "name:" + ((data && data.tool) || "");
}

function isEditTool(tool) {
  return ["edit", "patch", "write", "move", "remove"].includes(String(tool || "").toLowerCase());
}

function editCounts(meta) {
  const files = Array.isArray(meta && meta.files) ? meta.files : [];
  const added = Number(meta && (meta.line_added ?? meta.added ?? meta.added_lines)) ||
    files.reduce((n, f) => n + (Number(f.line_added) || 0), 0);
  const removed = Number(meta && (meta.line_removed ?? meta.removed ?? meta.removed_lines)) ||
    files.reduce((n, f) => n + (Number(f.line_removed) || 0), 0);
  return { added: Math.max(0, added), removed: Math.max(0, removed) };
}

function editTarget(data) {
  const args = (data && data.args) || {};
  return args.path || args.file || args.root || "working files";
}

let codePanelState = { path: "", text: "", lineStart: 0, lineEnd: 0 };

function openCodePanel(path, text, lineStart = 0, lineEnd = 0) {
  const panel = $("code-panel");
  if (!panel) return;
  codePanelState = { path: path || "working file", text: text || "", lineStart, lineEnd };
  $("code-panel-title").textContent = codePanelState.path;
  $("code-panel-meta").textContent = lineStart
    ? `lines ${lineStart}${lineEnd ? `–${lineEnd}` : ""}` : "live edit preview";
  $("code-panel-code").textContent = codePanelState.text;
  panel.classList.remove("hidden");
  requestAnimationFrame(() => panel.classList.add("open"));
}

function closeCodePanel() {
  const panel = $("code-panel");
  if (!panel) return;
  panel.classList.remove("open");
  setTimeout(() => panel.classList.add("hidden"), 300);
  $("code-ask").classList.add("hidden");
}

function updateCodePanelFromDraft(turn) {
  const text = turn.box.text || "";
  const info = draftEditInfo(text);
  if (!info) return;
  const args = info.args || {};
  let code = args.new_text || args.content || "";
  try { code = JSON.parse('"' + code.replace(/\\"/g, '"') + '"'); } catch { /* partial JSON */ }
  openCodePanel(editTarget(info), code || text, args.line_start, args.line_end);
}

function wireCodeSelection() {
  const code = $("code-panel-code");
  if (!code) return;
  code.addEventListener("mouseup", () => {
    const selection = window.getSelection();
    const text = String(selection || "").trim();
    if (!text) return $("code-ask").classList.add("hidden");
    const range = selection.getRangeAt(0).getBoundingClientRect();
    const btn = $("code-ask");
    btn.style.left = `${Math.max(8, range.left - code.getBoundingClientRect().left)}px`;
    btn.style.top = `${Math.max(8, range.bottom - code.getBoundingClientRect().top + 6)}px`;
    btn.classList.remove("hidden");
    btn.onclick = () => {
      const query = `File: ${codePanelState.path}\nSelected code:\n${text}\n\nWhat should I change?`;
      input.value = query;
      autogrow();
      input.focus();
      btn.classList.add("hidden");
    };
  });
}

function draftEditInfo(text) {
  const tool = (text.match(/"tool"\s*:\s*"([^"\\]+)"/) || [])[1] || "";
  if (!isEditTool(tool)) return null;
  const path = (text.match(/"(?:path|file|root)"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"/) || [])[1] || "working files";
  const oldText = (text.match(/"old_text"\s*:\s*"([\s\S]*?)(?<!\\)"/) || [])[1] || "";
  const newText = (text.match(/"new_text"\s*:\s*"([\s\S]*?)(?<!\\)"/) || [])[1] || "";
  const diff = (text.match(/"diff"\s*:\s*"([\s\S]*?)(?<!\\)"/) || [])[1] || "";
  let added = 0, removed = 0;
  if (oldText || newText) {
    const lines = (v) => v ? v.split(/\\n|\n/).length : 0;
    added = Math.max(0, lines(newText) - lines(oldText));
    removed = Math.max(0, lines(oldText) - lines(newText));
  } else if (diff) {
    added = diff.split(/\\n|\n/).filter((l) => /^\+(?!\+\+)/.test(l)).length;
    removed = diff.split(/\\n|\n/).filter((l) => /^-(?!- -)/.test(l)).length;
  } else if (tool === "write") {
    added = newText ? newText.split(/\\n|\n/).length : 0;
  }
  return { tool, args: { path }, metadata: { line_added: added, line_removed: removed } };
}

function updateDraftEdit(turn) {
  const info = draftEditInfo(turn.box.text || "");
  if (!info) return;
  updateCodePanelFromDraft(turn);
  if (!turn.draftLive) {
    const call = "draft-" + Date.now() + "-" + Math.random().toString(36).slice(2);
    toolRow("start", { ...info, call });
    turn.draftKey = toolKey({ call });
    turn.draftLive = liveTools[turn.draftKey];
  }
  const live = turn.draftLive;
  if (!live || !live.editMeta) return;
  const counts = editCounts(info.metadata);
  live.label.textContent = toolTitle(info.tool);
  if (live.subject) live.subject.textContent = editTarget(info);
  live.editMeta.innerHTML = `<b class="add">+${counts.added}</b> <b class="del">-${counts.removed}</b>`;
  if (window.anime && !reduceMotion()) {
    anime({ targets: live.editMeta, scale: [1.12, 1], duration: 180, easing: "easeOutQuad" });
  }
}

function looksLikeToolStream(text) {
  const source = String(text || "");
  // Hide only a model tool-call payload. Ordinary code, prose, or a JSON
  // example mentioning a `tool` field must remain visible in the answer.
  // Crucially, the payload must start the answer. A normal answer is allowed
  // to discuss tools or include a JSON example later in its prose/code block.
  const fenced = /^\s*```(?:json)?\s*\{[\s\S]*?["']tool["']\s*:\s*["'][^"']+["'][\s\S]*?["']args["']\s*:/i;
  const object = /^\s*\{[\s\S]*?["']tool["']\s*:\s*["'][^"']+["'][\s\S]*?["']args["']\s*:/i;
  return fenced.test(source) || object.test(source);
}

function hideRawToolStream(turn) {
  const stream = turn && turn.box && turn.box.stream;
  if (!stream || !looksLikeToolStream(turn.box.text)) return false;
  // The model's JSON/prose tool payload is transport, never chat content.
  // Hide it as soon as the first tool-shaped fragment arrives. If it is an
  // edit/patch, updateDraftEdit creates the animated preview; otherwise the
  // real tool_start event will create the normal block moments later.
  stream.textContent = "";
  stream.style.display = "none";
  turn.rawToolHidden = true;
  updateDraftEdit(turn);
  return true;
}

function adoptDraftTool(turn, data) {
  if (!turn.draftLive || !data || !isEditTool(data.tool)) return false;
  const oldKey = turn.draftKey;
  const newKey = toolKey(data);
  liveTools[newKey] = turn.draftLive;
  if (oldKey) delete liveTools[oldKey];
  turn.draftKey = newKey;
  turn.draftLive = liveTools[newKey];
  return true;
}

function toolTitle(tool) {
  const names = {read: "Чтение файла", write: "Запись файла", edit: "Изменение файла",
    patch: "Применение изменений", bash: "Команда", shell: "Команда",
    grep: "Поиск в файлах", glob: "Поиск файлов", list_directory: "Просмотр папки",
    web_search: "Поиск в интернете", web_fetch: "Открытие страницы", git: "Git",
    think: "Планирование", ask: "Вопрос", todo: "Задачи", subagent: "Субагент",
    mcp_customuze_beecode_ui: "Оформление BeeCode",
    mcp_customuze_beecode_instructions: "Инструкции BeeCode", skill_finder: "Поиск навыков"};
  return names[String(tool || "").toLowerCase()] || String(tool || "Инструмент");
}

function toolSubject(data) {
  const args = data && data.args || {};
  for (const key of ["path", "file", "root", "command", "pattern", "url", "query", "task", "id"]) {
    if (typeof args[key] === "string" && args[key].trim()) {
      return args[key].replace(/\s+/g, " ").trim();
    }
  }
  return "";
}

function makeToolBlock(tool, kind, labelText, data = {}) {
  const details = document.createElement("details");
  const editing = isEditTool(tool);
  details.className = "tool" + (editing ? " edit-tool" : "");
  details.dataset.tool = tool || "tool";
  const summary = document.createElement("summary");
  const dot = dotSpan(kind);
  const label = document.createElement("span");
  label.className = "tool-label";
  label.textContent = toolTitle(tool);
  summary.append(dot, label);
  const subject = document.createElement("span");
  subject.className = "tool-subject";
  subject.textContent = toolSubject(data);
  subject.title = subject.textContent;
  summary.append(subject);
  const status = document.createElement("span");
  status.className = "tool-status";
  status.textContent = kind === "run" ? "Выполняется" : kind === "fail" ? "Ошибка" : kind === "note" ? "Сведения" : "Готово";
  status.setAttribute("aria-live", "polite");
  summary.append(status);
  details.dataset.state = kind;
  if (editing) {
    const meta = document.createElement("span");
    meta.className = "edit-diff";
    const counts = editCounts(data.metadata);
    meta.innerHTML = `<b class="add">+${counts.added}</b> <b class="del">-${counts.removed}</b>`;
    summary.append(meta);
  }
  details.append(summary);
  animateToolDisclosure(details, summary);
  return { details, dot, label, subject, status,
    editMeta: summary.querySelector(".edit-diff") };
}

function attachSubagentView(details, output) {
  let info;
  try { info = JSON.parse(output); } catch { return false; }
  if (!info || !/^[a-f0-9]{12}$/.test(info.id || "") || info.status !== "running") return false;
  const panel = document.createElement("div");
  panel.className = "subagent-chat";
  const note = document.createElement("div");
  note.className = "subagent-note";
  note.textContent = "Субагент · только просмотр";
  const feed = document.createElement("div");
  feed.className = "subagent-feed";
  panel.append(note, feed);
  details.append(panel);
  let timer = null, loading = false;
  const update = async () => {
    if (!details.isConnected || !details.open) { clearInterval(timer); timer = null; return; }
    if (loading) return;
    loading = true;
    try {
      const child = await api("/api/subagent?id=" + encodeURIComponent(info.id));
      feed.replaceChildren();
      const add = (label, text, cls) => {
        if (!text) return;
        const row = document.createElement("div");
        row.className = "subagent-message " + cls;
        const heading = document.createElement("strong"); heading.textContent = label;
        const body = document.createElement("div"); body.textContent = text;
        row.append(heading, body); feed.append(row);
      };
      add("Задача", child.task, "task");
      let text = "";
      const flush = () => { if (text) add("Субагент", text, "answer"); text = ""; };
      for (const event of child.events || []) {
        if (event.type === "stream_delta" || event.type === "response") text += event.text || "";
        else if (event.type === "tool_start") { flush(); add("Инструмент", toolTitle(event.tool) + (toolSubject(event) ? " · " + toolSubject(event) : ""), "action"); }
        else if (event.type === "tool_end") { flush(); add(event.error ? "Ошибка инструмента" : "Результат инструмента", event.output, "action"); }
        else if (event.type === "error") { flush(); add("Ошибка", event.text, "action"); }
      }
      flush();
      if (child.result && !(child.events || []).some(e => e.type === "stream_delta" || e.type === "response")) add("Результат", child.result, "answer");
      const labels = {running: "Работает", done: "Завершил", error: "Ошибка", stopped: "Остановлен", unavailable: "Ожидание данных"};
      note.textContent = `Субагент · ${labels[child.status] || child.status} · только просмотр`;
      if (child.status !== "running" && child.status !== "unavailable") { clearInterval(timer); timer = null; }
    } catch { note.textContent = "Не удалось открыть историю субагента"; clearInterval(timer); timer = null; }
    finally { loading = false; }
  };
  details.addEventListener("toggle", () => {
    if (!details.open) { clearInterval(timer); timer = null; return; }
    update();
    if (!timer) timer = setInterval(update, 1000);
  });
  return true;
}

function finishToolBlock(live, data) {
  live.dot.className = "dot " + (data.error ? "fail" : "ok");
  live.details.dataset.state = data.error ? "fail" : "ok";
  live.status.textContent = data.error ? "Ошибка" : "Готово";
  updateActivity(live.details, data.tool, data.error ? "fail" : "ok");
  if (live.editMeta) {
    const counts = editCounts(data.metadata);
    live.editMeta.innerHTML = `<b class="add">+${counts.added}</b> <b class="del">-${counts.removed}</b>`;
  }
  if (data.output && !(data.tool === "subagent" && !data.error && attachSubagentView(live.details, data.output))) {
    const pre = document.createElement("pre");
    pre.textContent = data.output;
    live.details.append(pre);
    if (data.error) live.details.open = true;
  }
  if (window.anime && !reduceMotion()) {
    anime({ targets: live.details, scale: [1.015, 1], duration: 260, easing: "easeOutQuad" });
  }
  if (isEditTool(data.tool)) {
    setTimeout(closeCodePanel, 450);
  }
}

function appendToolBlock(details) {
  let group = currentToolGroup;
  if (!group || !group.isConnected) {
    group = document.createElement("details");
    group.className = "tool-group";
    const summary = document.createElement('summary'); summary.className = 'activity-summary';
    summary.innerHTML = '<span class="activity-dot"></span><span class="activity-label"></span><span class="activity-count"></span><span class="activity-arrow" aria-hidden="true">›</span>';
    const items = document.createElement('div'); items.className = 'tool-group-items';
    group.append(summary, items);
    animateActivityDisclosure(group, summary, items);
    chat.append(group);
    currentToolGroup = group;
  }
  if (pendingActivityLabel) { details.dataset.activity = pendingActivityLabel; pendingActivityLabel = ''; }
  group.querySelector('.tool-group-items').append(details);
  updateActivity(details, details.dataset.tool, details.dataset.state);
}

function toolRow(phase, data) {
  data = data || {};
  if (phase === "end") {
    const key = toolKey(data);
    const live = liveTools[key];
    if (live && live.details.isConnected) {
      finishToolBlock(live, data);
      delete liveTools[key];
      scrollDown();
      return;
    }
    if (live) delete liveTools[key];
    // Orphan end (no matching start): render as a finished card, like before.
    const fresh = makeToolBlock(data.tool, data.error ? "fail" : "ok",
      `${data.tool || ""} — ${data.error ? "ошибка" : "готово"}`, data);
    if (data.output) {
      const pre = document.createElement("pre");
      pre.textContent = data.output;
      fresh.details.append(pre);
      if (data.error) fresh.details.open = true;
    }
    appendToolBlock(fresh.details);
    scrollDown();
    return;
  }
  const live = makeToolBlock(data.tool, phase === "note" ? "note" : "run", data.tool || "", data);
  appendToolBlock(live.details);
  if (phase === "start") liveTools[toolKey(data)] = live;
  if (phase === "note") {
    const pre = document.createElement("pre");
    pre.textContent = data.message || data.tool || "";
    live.details.append(pre);
    live.details.open = true;
  }
  scrollDown();
}

function historyToolRow(content) {
  // Saved transcript rows look like "[tool result] tool=read error=False\n…":
  // show them as the same tidy rows live turns get, not as a raw dump.
  // Transcript rows never touch the live registry: a replay must not hijack
  // a block the current turn is still updating.
  const text = String(content || "");
  const name = (/tool=([A-Za-z0-9_]+)/.exec(text) || [])[1] || "tool";
  const failed = /error=True/.test(text);
  const body = text.replace(/^\[tool result\][^\n]*\n?/, "");
  const card = makeToolBlock(name, failed ? "fail" : "ok",
    `${name} — ${failed ? "ошибка" : "готово"}`);
  if (!(name === "subagent" && !failed && attachSubagentView(card.details, body))) {
    const pre = document.createElement("pre");
    pre.textContent = body || "(пусто)";
    card.details.append(pre);
  }
  if (failed) card.details.open = true;
  appendToolBlock(card.details);
  scrollDown();
}

function deniedRow(data) {
  const div = document.createElement("div");
  div.className = "denied";
  const span = document.createElement("span");
  span.textContent = data.message || ("Нет доступа: " + data.tool);
  const btn = document.createElement("button");
  btn.textContent = "Разрешить";
  btn.onclick = async () => {
    btn.disabled = true;
    try {
      await post("/api/allow", { tool: data.tool });
      btn.textContent = "Ок — спроси ещё раз";
    } catch {
      btn.textContent = "Не вышло";
      btn.disabled = false;
    }
  };
  div.append(span, btn);
  chat.append(div);
  scrollDown();
}

function askCard(id, question, meta) {
  if (meta && meta.kind === "path_grant") {
    return pathGrantCard(id, question, meta);
  }
  const div = document.createElement("div");
  div.className = "askcard";
  const q = document.createElement("div");
  q.className = "q";
  q.textContent = question;
  const row = document.createElement("div");
  row.className = "row";
  const field = document.createElement("input");
  field.placeholder = "Твой ответ…";
  const btn = document.createElement("button");
  btn.textContent = "Ответить";
  const send = async () => {
    const answer = field.value.trim() || "(без ответа)";
    btn.disabled = true;
    field.disabled = true;
    try {
      await post("/api/decide", { id, answer });
      btn.textContent = "Отправлено";
    } catch {
      btn.textContent = "Не вышло";
      btn.disabled = false;
      field.disabled = false;
    }
  };
  btn.onclick = send;
  field.onkeydown = (event) => { if (event.key === "Enter") send(); };
  row.append(field, btn);
  div.append(q, row);
  chat.append(div);
  scrollDown();
  setTimeout(() => field.focus(), 50);
}

function pathGrantCard(id, question, meta) {
  const div = document.createElement("div");
  div.className = "askcard path-grant-card";
  const q = document.createElement("div");
  q.className = "q";
  q.textContent = question;
  const note = document.createElement("div");
  note.className = "path-grant-note";
  note.textContent = meta.folder || "External folder";
  const row = document.createElement("div");
  row.className = "row";
  const buttons = [
    ["allow_once", "Allow once"], ["deny", "Deny"],
    ["always_allow", "Always allow"]
  ];
  buttons.forEach(([answer, label]) => {
    const btn = document.createElement("button");
    btn.textContent = label;
    btn.onclick = async () => {
      buttons.forEach(([_, __]) => { /* keep labels stable for the closure */ });
      row.querySelectorAll("button").forEach((b) => { b.disabled = true; });
      try {
        await post("/api/decide", { id, answer });
        btn.textContent = answer === "deny" ? "Denied" : "Applied";
        div.classList.add(answer === "deny" ? "denied" : "granted");
      } catch {
        row.querySelectorAll("button").forEach((b) => { b.disabled = false; });
        btn.textContent = "Retry";
      }
    };
    row.appendChild(btn);
  });
  div.append(q, note, row);
  chat.append(div);
  scrollDown();
}

function infoRow(text) {
  const div = document.createElement("div");
  div.className = "inforow";
  div.textContent = text;
  chat.append(div);
  scrollDown();
}

function cmdRow(command, text, isErr) {
  const div = document.createElement("div");
  div.className = "cmdout" + (isErr ? " err" : "");
  const head = document.createElement("div");
  head.className = "cmdhead";
  head.textContent = command;
  const pre = document.createElement("pre");
  pre.textContent = text || (isErr ? "не вышло" : "(пусто)");
  div.append(head, pre);
  chat.append(div);
  scrollDown();
}

/* ---------- slash commands (same doors as the terminal) ---------- */

const SLASH_COMMANDS = ["help", "pool", "providers", "key", "model", "models",
  "mode", "permissions", "allow", "lang", "instructions", "config", "stats",
  "usage", "token", "tools", "window", "about"];

async function runSlash(command) {
  userRow(command);
  try {
    const body = await post("/api/cmd", { command });
    cmdRow(command, body.output || body.error || "(пусто)", false);
  } catch (error) {
    // A 400 here is guidance, not a crash: unknown doors and terminal-only
    // commands answer with where they live — show that text, not a traceback.
    const body = error.body || {};
    cmdRow(command, body.output || body.error || String(error).slice(0, 220), true);
  }
  refreshAfterCmd(command);
}

async function refreshAfterCmd(command) {
  // Engine commands change what the composer shows — repaint without a reload.
  const name = (command.slice(1).split(/\s+/)[0] || "").toLowerCase();
  try {
    if (["model", "models", "provider", "providers", "key", "pool"].includes(name)) {
      await Promise.all([loadConfig(), loadModels()]);
    } else if (["mode", "permissions", "lang"].includes(name)) {
      await loadConfig();
    }
  } catch { /* paint keeps old values */ }
}

function slashHint() {
  const text = input.value;
  const menu = $("slashmenu");
  if (!menu) return;
  const match = /^\/([a-zа-яё]*)$/i.exec(text.trim());
  if (!match || streaming) {
    menu.classList.add("hidden");
    return;
  }
  const frag = match[1].toLowerCase();
  const found = SLASH_COMMANDS.filter((c) => c.startsWith(frag)).slice(0, 8);
  if (!found.length) {
    menu.classList.add("hidden");
    return;
  }
  menu.innerHTML = "";
  for (const name of found) {
    const row = document.createElement("div");
    row.className = "mi";
    row.textContent = "/" + name;
    row.onmousedown = (event) => {
      event.preventDefault();
      input.value = "/" + name + " ";
      input.focus();
      autogrow();
      slashHint();
    };
    menu.append(row);
  }
  menu.classList.remove("hidden");
}

/* ---------- context bar ---------- */

function paintBar(pct, tokens, limit, inputLimit = 0, estimated = false) {
  const fill = $("ctxfill");
  fill.innerHTML = "";
  const inner = document.createElement("div");
  const color = pct < 50 ? "var(--green)" : (pct < 80 ? "var(--yellow)" : "var(--red)");
  inner.style.background = color;
  fill.append(inner);
  const w = Math.max(0, Math.min(100, pct));
  if (window.anime && !reduceMotion()) {
    anime({ targets: inner, width: w + "%", duration: 500, easing: "easeOutCubic" });
  } else {
    inner.style.width = w + "%";
  }
  if (!limit) {
    $("ctxtext").textContent = "—";
    return;
  }
  const big = tokens >= 1000000 ? (tokens / 1000000).toFixed(1) + "M"
    : tokens >= 1000 ? Math.round(tokens / 1000) + "k" : String(tokens);
  const lim = limit >= 1000000 ? Math.round(limit / 1000000) + "M"
    : limit >= 1000 ? Math.round(limit / 1000) + "k" : String(limit);
  const cap = inputLimit >= 1000000 ? Math.round(inputLimit / 1000000) + "M"
    : inputLimit >= 1000 ? Math.round(inputLimit / 1000) + "k" : String(inputLimit);
  $("ctxtext").textContent = `Контекст: ≈${big} / ${estimated ? "~" : ""}${lim} · ${pct}%` +
    (inputLimit && inputLimit < limit ? ` · вход до ${cap}` : "");
  $("ctxtext").title = "Оценка токенов текущего запроса относительно полного окна модели. " +
    (estimated ? "Размер окна приблизительный и не подтверждён. " : "") +
    (inputLimit ? `Лимит входа BeeCode: ${inputLimit.toLocaleString()} токенов; лишняя история может быть сокращена.` : "");
}

/* ---------- chat flow ---------- */

function setBusy(busy) {
  streaming = busy;
  updateNavigation();
  $("send").classList.toggle("hidden", busy);
  $("stop").classList.toggle("hidden", !busy);
}

async function sendMessage() {
  if (streaming || activeAgentView) return;
  pressKick($("send"));
  let text = input.value.trim();
  const draft = input.value;
  const draftFiles = attached.slice();
  if (text.startsWith("/") && !text.startsWith("//")) {
    input.value = "";
    autogrow();
    const hint = $("slashmenu");
    if (hint) hint.classList.add("hidden");
    $("greet").classList.add("hide");
    await runSlash(text);
    return;
  }
  if (attached.length) {
    for (const file of attached) {
      const safe = file.content.replace(/```/g, "'''");
      const short = file.path.split(/[\\/]/).pop();
      text += `\n\nПрикреплён файл ${short}:\n\`\`\`\n${safe}\n\`\`\``;
    }
  }
  if (!text) return;
  const payload = JSON.stringify({ session_id: sessionId, message: text, model,
    reasoning_effort: $("effort-picker").classList.contains("hidden") ? "" : selectedEffort });
  if (new TextEncoder().encode(payload).length > 1024 * 1024) {
    infoRow("⚠ Сообщение и вложения превышают лимит 1 МиБ. Уменьши их размер.");
    return;
  }
  attached = [];
  renderChips();
  input.value = "";
  autogrow();
  $("greet").classList.add("hide");
  const pendingRow = userRow(text.length > 4000 ? text.slice(0, 4000) + "\n…(содержимое вложения сокращено)" : text);

  setBusy(true);
  const turn = { box: assistantBox(), hadText: false };
  const controller = new AbortController();
  streamAbort = controller;
  let accepted = false;
  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: payload,
      signal: controller.signal,
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      turn.box.div.remove();
      pendingRow.remove();
      if (!input.value) input.value = draft;
      attached = draftFiles.concat(attached);
      renderChips();
      autogrow();
      infoRow("⚠ " + (body.error || ("HTTP " + response.status)));
      setBusy(false);
      return;
    }
    accepted = true;
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let cut;
      while ((cut = buf.indexOf("\n\n")) >= 0) {
        const frame = buf.slice(0, cut);
        buf = buf.slice(cut + 2);
        onFrame(frame, turn);
      }
    }
  } catch (error) {
    if (error && error.name !== "AbortError") {
      if (!accepted) {
        pendingRow.remove();
        turn.box.div.remove();
        if (!input.value) input.value = draft;
        attached = draftFiles.concat(attached);
        renderChips();
        autogrow();
      }
      infoRow("⚠ " + String(error).slice(0, 160));
    }
  } finally {
    if (turn.box && !turn.box.text && turn.box.stream.querySelector(".typing")) turn.box.div.remove();
    setBusy(false);
    streamAbort = null;
  }
}

function flushBox(turn) {
  // A text segment followed by a tool call is finished: render it where it
  // stands, so the turn reads top-down — intro, tool, result — instead of
  // piling every word into the first box above the tools.
  const box = turn.box;
  if (turn.rawToolHidden) return;
  if (box && box.text && !box.flushed) {
    const node = renderAssistant(box.text);
    box.stream.replaceWith(node);
    box.flushed = true;
    turn.hadText = true;
    scrollDown();
    stampIn(node);
  }
}

function onFrame(frame, turn) {
  const box = turn.box;
  let type = "", payload = "";
  for (const line of frame.split("\n")) {
    if (line.startsWith("event:")) type = line.slice(6).trim();
    else if (line.startsWith("data:")) payload += line.slice(5).trim();
  }
  let data = {};
  try { data = JSON.parse(payload || "{}"); } catch { /* keep sick */ }
  if (type === "start") {
    sessionId = data.session_id || sessionId;
    rememberChat(sessionId);
    refreshSessions();
  } else if (type === "delta") {
    // Providers can stream reasoning separately; it is not part of the answer.
    if (data.kind === "reasoning_delta") return;
    turn.activityRaw = (turn.activityRaw || '') + (data.text || '');
    const activity = extractActivity(turn.activityRaw);
    if (activity.label && turn.activitySeen !== activity.sequence) {
      turn.activitySeen = activity.sequence; applyActivityLabel(activity.label);
    }
    if (!activity.text.trim() && activity.label) return;
    if (!activity.text) return;
    const box = ensureTurnBox(turn);
    box.text = activity.text;
    const rawTool = hideRawToolStream(turn);
    if (rawTool) box.stream.style.display = "none";
    if (box.stream.querySelector(".typing")) box.stream.innerHTML = "";
    if (!rawTool) renderStreaming(box);
    scrollDown();
  } else if (type === "tool") {
    if (turn.box) flushBox(turn);
    // A pristine "waiting" box (typing dots, no text yet) right before a tool
    // block is pure noise: the tool block itself is the progress indicator.
    // Drop it and point the turn at a fresh box afterwards.
    if (turn.box && !turn.box.text && turn.box.stream &&
        turn.box.stream.querySelector(".typing")) {
      turn.box.div.remove();
    }
    if (turn.box && turn.rawToolHidden) turn.box.div.remove();
    if (!(data.phase === "start" && adoptDraftTool(turn, data))) {
      toolRow(data.phase, data);
    }
    // Do not create an empty assistant card after a tool. The next delta
    // creates it lazily; this prevents duplicate blank cards above/below the
    // real tool card.
    turn.box = null;
    turn.activityRaw = ''; turn.activitySeen = 0;
    if (data.phase === "end") ensureTurnBox(turn);
  } else if (type === "denied") {
    if (turn.box) flushBox(turn);
    deniedRow(data);
    turn.box = null;
  } else if (type === "ask") {
    if (turn.box) flushBox(turn);
    askCard(data.id, data.question, data.meta || {});
    turn.box = null;
  } else if (type === "done") {
    const finalActivity = extractActivity(data.text || '', true);
    if (finalActivity.label && turn.activitySeen !== finalActivity.sequence) applyActivityLabel(finalActivity.label);
    if (!turn.box && finalActivity.text.trim() && !turn.hadText) ensureTurnBox(turn);
    if (!turn.box) return;
    const tail = turn.box.text;
    if (turn.rawToolHidden) {
      // A tool-shaped answer is transport, not an assistant message. If a
      // broken endpoint never emits tool_start, never leak that raw JSON back
      // into the chat at done-time.
      turn.box.div.remove();
    } else if (tail) {
      const node = renderAssistant(tail);
      turn.box.stream.replaceWith(node);
      turn.hadText = true;
      scrollDown();
      stampIn(node);
    } else if (finalActivity.text.trim() && !turn.hadText) {
      const node = renderAssistant(finalActivity.text);
      turn.box.stream.replaceWith(node);
      scrollDown();
      stampIn(node);
    } else {
      // The turn ended on a tool call (or the server repeated the whole
      // answer): no empty pendant box, no duplicated text.
      if (turn.box.stream.querySelector(".typing")) turn.box.stream.innerHTML = "";
      turn.box.div.remove();
    }
  } else if (type === "title") {
    if (sessionId && typeof data.title === "string" && data.title.trim()) {
      titles[sessionId] = data.title.trim().slice(0, 80);
      drawSessions(sessionCache);
    }
  } else if (type === "ready") {
    if (data.session_id) sessionId = data.session_id;
    if (data.usage) paintBar(data.usage.contextPct ?? data.usage.pct ?? 0, data.usage.tokens || 0, data.usage.contextWindow || data.usage.limit || 0, data.usage.limit || 0, data.usage.contextEstimated);
    refreshSessions();
  } else if (type === "error") {
    infoRow("❌ " + (data.message || "error"));
  } else if (type === "stopped") {
    infoRow("Остановлено.");
  } else if (type === "info" && data.name === "context_trimmed") {
    infoRow("История ужата под окно.");
  }
}

/* ---------- project library ---------- */
let selectedProject = "", selectedFolder = "";
let projectsCache = [], projectsFingerprint = "", projectFormState = null;
let projectFormOrigin = null;
let activeAgentView = "", agentViewTimer = null, agentViewToken = 0;
const expandedProjects = new Set(), expandedFolders = new Set();

function startProjectForm(kind, project = "", parent = "") {
  projectFormOrigin = document.activeElement;
  projectFormState = {kind, project, parent};
  const projectInfo = projectsCache.find(item => item.id === project);
  const parentInfo = projectInfo?.folders?.find(item => item.id === parent);
  const containerName = parentInfo?.name || projectInfo?.name;
  $("project-dialog-title").textContent = kind === "project" ? "Создать проект" : "Создать папку";
  $("project-dialog-description").textContent = kind === "project"
    ? "Объединяйте чаты и группы по одной задаче. Субагенты проекта тоже появятся здесь."
    : "Папки помогают упорядочить чаты внутри BeeCode и не создают папки на компьютере.";
  $("project-form").querySelector('[type="submit"]').textContent = kind === "project" ? "Создать проект" : "Создать папку";
  $("project-form-label").textContent = kind === "project" ? "Название проекта" :
    containerName ? `Папка в «${containerName}»` : "Название папки";
  $("project-name").placeholder = kind === "project" ? "Название проекта" : "Название папки";
  $("project-name").removeAttribute("aria-invalid");
  $("project-error").textContent = "";
  $("project-name").value = "";
  $("project-dialog").classList.remove("hidden");
  $("project-name").focus();
}
function closeProjectForm() {
  projectFormState = null; $("project-dialog").classList.add("hidden");
  if (projectFormOrigin?.isConnected) projectFormOrigin.focus();
  else $("project-create").focus();
  projectFormOrigin = null;
}
$("project-create").onclick = () => startProjectForm("project");
$("project-cancel").onclick = closeProjectForm;
$("project-dialog-close").onclick = () => { if (!$("project-cancel").disabled) closeProjectForm(); };
$("project-dialog").addEventListener("click", event => {
  if (event.target === $("project-dialog") && !$("project-cancel").disabled) closeProjectForm();
});
$("project-dialog").addEventListener("keydown", event => {
  if (event.key === "Escape" && !$("project-cancel").disabled) {
    event.preventDefault(); closeProjectForm();
  }
  if (event.key === "Tab") {
    const controls = [...$("project-dialog").querySelectorAll('button:not(:disabled), input:not(:disabled)')];
    if (!controls.length) return;
    const first = controls[0], last = controls[controls.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }
});
$("project-name").addEventListener("input", () => {
  $("project-name").removeAttribute("aria-invalid"); $("project-error").textContent = "";
});
$("project-form").onsubmit = async (event) => {
  event.preventDefault();
  if (!projectFormState) return;
  const state = projectFormState, name = $("project-name").value.trim();
  if (!name) { $("project-error").textContent = "Введите название"; $("project-name").setAttribute("aria-invalid", "true"); $("project-name").focus(); return; }
  const submit = $("project-form").querySelector('[type="submit"]'); submit.disabled = true;
  $("project-cancel").disabled = true;
  try {
    const payload = state.kind === "project" ? {action: "create", name} :
      {action: "folder", project: state.project, parent: state.parent, name};
    const result = await post("/api/projects", payload);
    if (state.kind === "project") {
      selectedProject = result.item.id; selectedFolder = ""; expandedProjects.add(result.item.id);
    } else {
      selectedProject = state.project; selectedFolder = result.item.id;
      expandedProjects.add(state.project); if (state.parent) expandedFolders.add(state.parent);
    }
    closeProjectForm(); await refreshProjects(true);
  } catch (error) { $("project-error").textContent = error.message || "Не удалось сохранить"; }
  finally { submit.disabled = false; $("project-cancel").disabled = false; }
};
function projectButton(text, title, action, className = "") {
  const button = document.createElement("button"); button.type = "button"; button.className = className;
  button.textContent = text; button.title = title; button.setAttribute("aria-label", title);
  button.onclick = action; return button;
}
function drawProjects() {
  const list = $("projects-list"); list.replaceChildren();
  if (!projectsCache.length) {
    const empty = document.createElement("div"); empty.className = "project-empty";
    empty.textContent = "Нет проектов"; list.append(empty); return;
  }
  const sessionById = new Map(sessionCache.map(row => [row.id, row]));
  for (const project of projectsCache) {
    const box = document.createElement("div"); box.className = "project-block";
    function header(name, folderId, depth, isProject) {
      const line = document.createElement("div"); line.className = "project-line";
      line.style.setProperty("--depth", Math.min(depth, 5));
      if (project.id === selectedProject && folderId === selectedFolder) line.classList.add("selected");
      const set = isProject ? expandedProjects : expandedFolders, key = isProject ? project.id : folderId;
      const open = set.has(key);
      const toggle = projectButton(open ? "▾" : "▸", open ? "Свернуть" : "Развернуть", () => {
        if (set.has(key)) set.delete(key); else set.add(key); drawProjects();
      }, "project-toggle");
      const select = projectButton(name, isProject ? `Проект: ${name}` : `Папка: ${name}`, () => {
        selectedProject = project.id; selectedFolder = folderId; set.add(key); drawProjects();
      }, "project-name");
      const addFolder = projectButton("+", "Создать папку", () => startProjectForm("folder", project.id, folderId), "project-action");
      const addChat = projectButton("✎", "Создать чат", () => {
        selectedProject = project.id; selectedFolder = folderId; set.add(key); newChat({project: project.id, folder: folderId});
      }, "project-action");
      line.append(toggle, select, addFolder, addChat); return {line, open};
    }
    const root = header(project.name, "", 0, true); box.append(root.line);
    function children(parent, depth) {
      if (depth > 12) return;
      for (const folder of project.folders || []) {
        if ((folder.parent || "") !== parent) continue;
        const item = header(folder.name, folder.id, depth, false); box.append(item.line);
        if (item.open) children(folder.id, depth + 1);
      }
      for (const entry of project.chats || []) {
        if ((entry.folder || "") !== parent) continue;
        const info = sessionById.get(entry.id) || {};
        const name = info.title || titles[entry.id] || "Чат " + chatDate(entry.id);
        const line = projectButton(name, `Открыть чат: ${name}`, () => {
          selectedProject = project.id; selectedFolder = parent; switchSession(entry.id);
        }, "project-chat");
        line.style.setProperty("--depth", Math.min(depth, 5));
        if (entry.id === sessionId && !activeAgentView) line.classList.add("selected");
        box.append(line);
      }
      for (const child of project.agents || []) {
        if ((child.folder || "") !== parent) continue;
        const name = (child.task || "Субагент").replace(/\s+/g, " ").slice(0, 48);
        const labels = {running: "работает", done: "завершил", error: "ошибка", stopped: "остановлен"};
        const line = projectButton(name, `Субагент: ${name} · ${labels[child.status] || child.status}`, () => openAgentView(child.id), "project-agent");
        line.dataset.state = child.status || "";
        line.style.setProperty("--depth", Math.min(depth, 5));
        if (child.id === activeAgentView) line.classList.add("selected");
        box.append(line);
      }
    }
    if (root.open) children("", 1);
    list.append(box);
  }
}
async function refreshProjects(force = false) {
  try {
    const body = await api("/api/projects");
    const items = Array.isArray(body.projects) ? body.projects : [];
    const fingerprint = JSON.stringify(items);
    if (!force && (fingerprint === projectsFingerprint || projectFormState)) return;
    projectsCache = items; projectsFingerprint = fingerprint; drawProjects();
  } catch (error) {
    if (force) { const list = $("projects-list"); list.textContent = "Не удалось загрузить проекты: " + error.message; }
  }
}
function closeAgentView() {
  activeAgentView = ""; agentViewToken++;
  clearInterval(agentViewTimer); agentViewTimer = null;
  $("agent-view").classList.add("hidden"); $("composer-wrap").classList.remove("hidden");
  drawProjects();
}
$("agent-view-close").onclick = closeAgentView;
async function openAgentView(id) {
  activeAgentView = id; const token = ++agentViewToken;
  clearInterval(agentViewTimer);
  $("agent-view").classList.remove("hidden"); $("composer-wrap").classList.add("hidden");
  $("agent-view-feed").textContent = "Загружаю…";
  drawProjects();
  let loading = false;
  const refresh = async () => {
    if (loading || activeAgentView !== id || agentViewToken !== token) return;
    loading = true;
    try {
      const child = await api("/api/subagent?id=" + encodeURIComponent(id));
      if (activeAgentView !== id || agentViewToken !== token) return;
      const labels = {running:"Работает", done:"Завершил", error:"Ошибка", stopped:"Остановлен", interrupted:"Прерван", unavailable:"Ожидание данных"};
      $("agent-view-title").textContent = "Субагент";
      $("agent-view-status").textContent = `${labels[child.status] || child.status} · только просмотр`;
      const feed = $("agent-view-feed"); feed.replaceChildren();
      const add = (label, value, type) => {
        if (!value) return;
        const block = document.createElement("div"); block.className = "agent-entry " + type;
        const head = document.createElement("strong"); head.textContent = label;
        const text = document.createElement("div"); text.textContent = value;
        block.append(head, text); feed.append(block);
      };
      add("Задача", child.task, "task");
      let text = "";
      const flush = () => { if (text) add("Субагент", text, "answer"); text = ""; };
      for (const event of child.events || []) {
        if (event.type === "stream_delta" || event.type === "response") text += event.text || "";
        else if (event.type === "tool_start") { flush(); add("Инструмент", toolTitle(event.tool) + (toolSubject(event) ? " · " + toolSubject(event) : ""), "action"); }
        else if (event.type === "tool_end") { flush(); add(event.error ? "Ошибка инструмента" : "Результат инструмента", event.output, "action"); }
        else if (event.type === "error") { flush(); add("Ошибка", event.text, "action"); }
      }
      flush();
      if (child.result && !(child.events || []).some(e => e.type === "stream_delta" || e.type === "response")) add("Результат", child.result, "answer");
      if (child.status !== "running" && child.status !== "unavailable") { clearInterval(agentViewTimer); agentViewTimer = null; }
    } catch (error) {
      if (activeAgentView === id && agentViewToken === token) {
        $("agent-view-feed").textContent = "Не удалось открыть субагента: " + error.message;
        clearInterval(agentViewTimer); agentViewTimer = null;
      }
    } finally { loading = false; }
  };
  agentViewTimer = setInterval(refresh, 1000);
  await refresh();
}
setInterval(() => { if (!projectFormState) refreshProjects(); }, 2000);
refreshProjects(true);

/* ---------- sessions ---------- */

function chatDate(id) {
  const match = /^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})/.exec(id || "");
  if (!match) return id;
  return `${match[3]}.${match[2]} ${match[4]}:${match[5]}`;
}

function drawSessions(items) {
  sessionsBox.innerHTML = "";
  const query = ($("search").value || "").toLowerCase();
  for (const item of items || []) {
    const title = item.title || titles[item.id] || ("Чат " + chatDate(item.id));
    if (query && !title.toLowerCase().includes(query)) continue;
    const div = document.createElement("div");
    div.className = "session" + (item.id === sessionId ? " active" : "");
    const head = document.createElement("div");
    head.className = "t";
    head.textContent = title.slice(0, 48);
    const meta = document.createElement("div");
    meta.className = "meta";
    const count = Number(item.messages) || 0;
    meta.textContent = count > 0 ? `${chatDate(item.id)} · ${count}` : chatDate(item.id);
    div.append(head, meta);
    div.onclick = () => switchSession(item.id);
    sessionsBox.append(div);
  }
}

let sessionCache = [];

async function refreshSessions() {
  try {
    const body = await api("/api/sessions");
    sessionCache = body.sessions || [];
    drawSessions(sessionCache);
    drawProjects();
    for (const item of sessionCache) {
      if (item.title) { titles[item.id] = item.title; continue; }
      if (titles[item.id]) continue;
      try {
        const full = await api("/api/session?id=" + encodeURIComponent(item.id));
        const first = (full.messages || []).find((m) => m.role === "user");
        if (first && first.content) {
          titles[item.id] = first.content.replace(/\s+/g, " ").slice(0, 48);
          drawSessions(sessionCache);
        }
      } catch { /* title stays default */ }
    }
  } catch { /* sidebar keeps old list */ }
}

function historyAssistantText(content, followedByTool) {
  const text = String(content || "");
  if (!text.trim()) return "";
  if (!followedByTool) return text;
  const fenced = /^\s*```(?:json)?\s*([\s\S]*?)```\s*/i.exec(text);
  const candidate = fenced ? fenced[1] : text.trim();
  try {
    const payload = JSON.parse(candidate);
    if (payload && typeof payload === "object" && typeof payload.tool === "string" &&
        payload.args && typeof payload.args === "object" && !Array.isArray(payload.args)) {
      return fenced ? text.slice(fenced[0].length) : "";
    }
  } catch { /* Keep content the parser cannot safely identify. */ }
  return text;
}

async function switchSession(id, remember = true) {
  if (streaming) return;
  closeAgentView();
  try {
    const body = await api("/api/session?id=" + encodeURIComponent(id));
    sessionId = body.id;
    if (remember) rememberChat(sessionId);
    followChat = true;
    chat.innerHTML = "";
    $("greet").classList.toggle("hide", (body.messages || []).length > 0);
    const messages = body.messages || [];
    for (let index = 0; index < messages.length; index++) {
      const message = messages[index];
      if (message.role === "user") userRow(message.content);
      else if (message.role === "assistant") {
        const activity = extractActivity(message.content, true);
        if (activity.label) applyActivityLabel(activity.label);
        const content = historyAssistantText(activity.text, messages[index + 1]?.role === "tool");
        if (!content.trim()) continue;
        const box = assistantBox();
        box.stream.replaceWith(renderAssistant(content));
      } else if (message.role === "tool") {
        historyToolRow(message.content);
      }
    }
    refreshSessions();
    try {
      const usage = await api("/api/usage?id=" + encodeURIComponent(id));
      paintBar(usage.contextPct ?? usage.pct ?? 0, usage.tokens || 0, usage.contextWindow || usage.limit || 0, usage.limit || 0, usage.contextEstimated);
    } catch { /* bar keeps old value */ }
    return true;
  } catch (error) {
    infoRow("⚠ " + String(error).slice(0, 160));
    return false;
  }
}

async function newChat(projectPlace = null) {
  if (streaming) return;
  closeAgentView();
  try {
    const place = projectPlace && typeof projectPlace.project === "string" ? projectPlace : {};
    if (!place.project) { selectedProject = ""; selectedFolder = ""; }
    const body = await post("/api/sessions/new", place);
    sessionId = body.id;
    rememberChat(sessionId);
    chat.innerHTML = "";
    $("greet").classList.remove("hide");
    paintBar(0, 0, 0);
    refreshSessions();
    if (place.project) await refreshProjects(true);
    input.focus();
    scrambleText($("greet-text"), $("greet-text").textContent);
  } catch (error) {
    infoRow("⚠ " + String(error).slice(0, 160));
  }
}

/* ---------- models / modes / trust / attach ---------- */

function fmtWindow(size) {
  if (!size) return "";
  return size >= 1000000 ? Math.round(size / 1000000) + "M" : Math.round(size / 1000) + "k";
}

async function loadModels() {
  try {
    const body = await api("/api/models");
    models = body.models || [];
    modelsProvider = body.provider || "";
    const menu = $("modelmenu");
    menu.innerHTML = "";
    if (!models.length) {
      $("effort-picker").classList.add("hidden");
      $("modelwarn").classList.remove("hidden");
      $("modelwarn-text").textContent = "Нет моделей. Проверь провайдера.";
      return;
    }
    for (const item of models) {
      const row = document.createElement("div");
      row.className = "mi" + (item.id === model ? " on" : "");
      const ico = document.createElement("img");
      ico.src = brandIconPath(detectBrand(item.id).icon);
      ico.alt = "";
      attachIconFallback(ico);
      const name = document.createElement("span");
      name.textContent = item.name || item.id;
      const meta = document.createElement("small");
      meta.textContent = `окно ${item.measured ? "✔ " : "~"}${fmtWindow(item.window)}${item.reasoning ? " · reasoning" : ""}`;
      meta.title = "Полное окно модели; приблизительное значение может быть непроверенным. Лимит входа BeeCode показан на полосе контекста и может быть меньше.";
      if (item.reasoning && item.efforts?.length) meta.title += " Уровни рассуждения: " + item.efforts.join(", ") + ".";
      row.append(ico, name, meta);
      row.onclick = () => {
        model = item.id;
        setActiveModel(item.id);
        menu.classList.add("hidden");
        [...menu.children].forEach((el) => el.classList.remove("on"));
        row.classList.add("on");
      };
      menu.append(row);
    }
    if (body.note) {
      $("modelwarn").classList.remove("hidden");
      $("modelwarn-text").textContent = body.note;
    } else if (models.length) {
      $("modelwarn").classList.add("hidden");
    }
    if (!model && models.length) model = models[0].id;
    setActiveModel(model);
  } catch { /* picker keeps placeholder */ }
}

function shortModel(id) {
  if (!id) return "…";
  return id.length > 26 ? id.slice(0, 25) + "…" : id;
}

const PERM_LABELS = { ask: "Спрашивать", auto: "Авто", readonly: "Чтение" };

function paintMode() {
  if (!PERM_LABELS[permMode]) permMode = "ask";
  $("permmode").textContent = PERM_LABELS[permMode];
  document.querySelectorAll("#set-modes button").forEach((btn) => {
    btn.classList.toggle("on", btn.dataset.mode === permMode);
  });
}

async function setMode(mode) {
  try {
    const body = await post("/api/config", { permissions: mode });
    if (!body.changed || !body.changed.includes("permissions")) {
      throw new Error("not applied");
    }
    permMode = mode;
  } catch {
    infoRow("Не вышло переключить режим.");
    return;
  }
  paintMode();
}

function renderChips() {
  const box = $("chips");
  box.innerHTML = "";
  for (const file of attached) {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.textContent = file.path.split(/[\\/]/).pop() + " ";
    const kill = document.createElement("button");
    kill.textContent = "✕";
    kill.onclick = () => {
      attached = attached.filter((f) => f !== file);
      renderChips();
    };
    chip.append(kill);
    box.append(chip);
  }
}

async function attachFiles() {
  if (!window.beeDesk || !window.beeDesk.attach) {
    infoRow("Прикрепление доступно в приложении BeeCode Desktop.");
    return;
  }
  try {
    const files = await window.beeDesk.attach();
    for (const file of files || []) attached.push(file);
    renderChips();
    input.focus();
  } catch { /* dialog closed */ }
}

async function checkTrust() {
  try {
    const body = await api("/api/trust");
    if (body.trusted) return;
    const banner = $("trust");
    const claims = $("trust-claims");
    claims.innerHTML = "";
    for (const line of body.claims || []) {
      const div = document.createElement("div");
      div.textContent = "• " + line;
      claims.append(div);
    }
    banner.classList.remove("hidden");
    $("trust-yes").onclick = async () => {
      await post("/api/trust", { answer: "yes" });
      banner.classList.add("hidden");
    };
    $("trust-no").onclick = async () => {
      await post("/api/trust", { answer: "no" });
      banner.classList.add("hidden");
    };
  } catch { /* offline backend shows fatal below */ }
}

async function loadConfig() {
  try {
    const body = await api("/api/config");
    if (body.model) {
      model = body.model;
      setActiveModel(model);
    }
    if (body.permissions) permMode = body.permissions;
    if (body.workdir) {
      workdir = body.workdir;
      const base = body.workdir.split(/[\\/]/).filter(Boolean).pop();
      $("project").textContent = base || body.workdir;
      $("project").title = body.workdir;
      $("projchip-name").textContent = base || body.workdir;
    }
  } catch { /* defaults stand */ }
  paintMode();
}

/* ---------- theme ---------- */

function applyTheme(name, persist = true) {
  const theme = name === "zinc" ? "zinc" : "paper";
  document.documentElement.dataset.theme = theme;
  if (window.beeDesk?.setTitlebarTheme) window.beeDesk.setTitlebarTheme(theme, currentSkin);
  try {
    if (persist) localStorage.setItem("beecode-theme", theme);
  } catch { /* private mode keeps paper */ }
  document.querySelectorAll("#appearance-theme button").forEach((btn) => {
    const active = btn.dataset.v === theme;
    btn.classList.toggle("on", active);
    btn.setAttribute("aria-pressed", String(active));
  });
}

function toggleTheme() {
  applyTheme(document.documentElement.dataset.theme === "zinc" ? "paper" : "zinc");
  stampNow("beecode-theme-ts");
}

/* ---------- skins (brutal / paper) ---------- */

let currentSkin = "brutal";

function applySkin(name, persist = true) {
  const skin = name === "paper" ? "paper" : "brutal";
  currentSkin = skin;
  document.documentElement.dataset.skin = skin;
  if (window.beeDesk?.setTitlebarTheme) window.beeDesk.setTitlebarTheme(document.documentElement.dataset.theme || "paper", skin);
  const link = document.getElementById("skinlink");
  if (link) {
    const href = "skins/" + skin + ".css";
    if (link.getAttribute("href") !== href) link.setAttribute("href", href);
  }
  try {
    if (persist) localStorage.setItem("beecode-skin", skin);
  } catch { /* private mode keeps brutal */ }
  document.querySelectorAll("#appearance-skin button").forEach((btn) => {
    const active = btn.dataset.v === skin;
    btn.classList.toggle("on", active);
    btn.setAttribute("aria-pressed", String(active));
  });
}

function toggleSkin() {
  applySkin(currentSkin === "paper" ? "brutal" : "paper");
  stampNow("beecode-skin-ts");
  if (window.anime && !reduceMotion()) {
    anime({
      targets: "#app",
      opacity: [0.25, 1],
      scale: [0.995, 1],
      duration: 380,
      easing: "easeOutQuad"
    });
  }
}

/* ---------- motion (anime.js + gsap + confetti) ---------- */

const reduceMotion = () =>
  window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/* ---------- agent-driven interface (the `ui` tool) ---------- */

let appliedUiVersion = -1;
let appliedUiTimestamp = -1;
let agentHiddenIds = [];
let agentAccent = null;

function applyAgentHidden() {
  for (const id of agentHiddenIds) {
    const element = document.getElementById(id) || document.getElementById('bee-extra-' + id);
    if (element) element.classList.add('ui-agent-hidden');
  }
}

function applyAgentComponents(prefs) {
  document.querySelectorAll('.ui-agent-hidden').forEach((el) => el.classList.remove('ui-agent-hidden'));
  document.querySelectorAll('[data-bee-extra]').forEach((el) => el.remove());
  for (const item of Array.isArray(prefs.components) ? prefs.components : []) {
    if (!item || !/^[A-Za-z][A-Za-z0-9_-]{0,63}$/.test(item.id) ||
        !['sidebar', 'main', 'composer-wrap'].includes(item.slot) ||
        !['text', 'notice', 'button'].includes(item.kind) || typeof item.text !== 'string') continue;
    const slot = document.getElementById(item.slot);
    if (!slot) continue;
    const el = document.createElement(item.kind === 'button' ? 'button' : 'div');
    el.id = 'bee-extra-' + item.id;
    el.dataset.beeExtra = 'true';
    el.className = 'ui-agent-extra ui-agent-extra-' + item.kind;
    el.textContent = item.text.slice(0, 1000);
    if (item.kind === 'button') {
      el.type = 'button';
      if (item.button_action === 'settings') el.onclick = openSettings;
      else if (item.button_action === 'new_chat') el.onclick = newChat;
      else continue;
    }
    slot.append(el);
  }
  agentHiddenIds = (Array.isArray(prefs.hiddenComponents) ? prefs.hiddenComponents : [])
    .filter((id) => typeof id === 'string' && /^[A-Za-z][A-Za-z0-9_-]{0,63}$/.test(id));
  applyAgentHidden();
}

new MutationObserver(() => applyAgentHidden()).observe(document.body, {childList: true, subtree: true});

async function resetAgentUi() {
  try {
    await post('/api/ui/reset', {});
    appliedUiVersion = -1;
    await pollUiPrefs();
    infoRow('Изменения интерфейса от агента сброшены.');
  } catch (error) { infoRow('Не удалось сбросить оформление: ' + String(error)); }
}

function lsNum(key) {
  try {
    const v = parseFloat(localStorage.getItem(key) || "0");
    return Number.isFinite(v) ? v : 0;
  } catch { return 0; }
}

function stampNow(key) {
  try { localStorage.setItem(key, String(Date.now() / 1000)); } catch { /* ignore */ }
}

function applyUiCss(version) {
  let link = document.getElementById("uicss");
  if (!link) {
    link = document.createElement("link");
    link.id = "uicss";
    link.rel = "stylesheet";
    document.head.appendChild(link);
  }
  link.setAttribute("href", "/api/ui-css?v=" + encodeURIComponent(version));
}

function dropUiCss() {
  const link = document.getElementById("uicss");
  if (link) link.remove();
}

function applyUiPrefs(prefs) {
  // Last writer wins: the agent's updated_at against the local toggle
  // timestamps (same machine, so the clocks compare). Manual choice beats
  // an older agent write; a newer agent write beats a manual choice.
  if (!prefs || typeof prefs.version !== "number") return;
  appliedUiVersion = prefs.version;
  appliedUiTimestamp = Number(prefs.updated_at) || 0;
  applyAgentComponents(prefs);
  const ts = Number(prefs.updated_at) || 0;
  if (prefs.skin && ts >= lsNum("beecode-skin-ts")) applySkin(prefs.skin, false);
  if (prefs.theme && ts >= lsNum("beecode-theme-ts")) applyTheme(prefs.theme, false);
  if (prefs.version === 0) {
    try {
      applySkin(localStorage.getItem("beecode-skin") || "brutal", false);
      applyTheme(localStorage.getItem("beecode-theme") || "paper", false);
    } catch { /* keep the current choices */ }
    if (model) paintBrand(model);
  }
  const previousAccent = agentAccent;
  agentAccent = typeof prefs.accent === "string" && /^#(?:[0-9a-f]{3}|[0-9a-f]{6})$/i.test(prefs.accent)
    ? prefs.accent : null;
  const root = document.documentElement;
  if (agentAccent) {
    root.dataset.agentAccent = "true";
    setAccent(agentAccent);
  } else {
    delete root.dataset.agentAccent;
    root.style.removeProperty("--agent-accent-fg");
    if (previousAccent) {
      if (model) paintBrand(model);
      else { root.style.removeProperty("--amber"); root.style.removeProperty("--amber-deep"); }
    }
  }
  if (prefs.customCss) applyUiCss(prefs.version);
  else dropUiCss();
}

async function pollUiPrefs() {
  try {
    const body = await api("/api/ui");
    if (body && typeof body.version === "number" &&
        (body.version !== appliedUiVersion || (Number(body.updated_at) || 0) !== appliedUiTimestamp)) {
      applyUiPrefs(body);
    }
  } catch { /* offline keeps the current look */ }
}

function popIn(node) {
  if (!node || !window.anime || reduceMotion()) return;
  anime({
    targets: node,
    scale: [0.94, 1.03, 1],
    translateY: [12, 0],
    duration: 480,
    easing: "easeOutQuad"
  });
}

function stampIn(node) {
  if (!node || !window.anime || reduceMotion()) return;
  anime({
    targets: node,
    scale: [1.06, 1],
    rotate: [-1.3, 0],
    duration: 360,
    easing: "easeOutQuad"
  });
}

function pressKick(node) {
  if (!node || !window.anime || reduceMotion()) return;
  anime({
    targets: node,
    scale: [1, 0.82, 1.06, 1],
    duration: 320,
    easing: "easeOutQuad"
  });
}

function celebrate() {
  if (!window.confetti || reduceMotion()) return;
  const accent = getComputedStyle(document.documentElement).getPropertyValue("--amber").trim() || "#ff3d00";
  confetti({
    particleCount: 90,
    spread: 75,
    startVelocity: 32,
    origin: { y: 0.62 },
    colors: [accent, "#161616", "#ffffff", "#ffd166", "#0a7c3b"]
  });
}

function scrambleText(el, finalText) {
  if (!el) return;
  if (!window.anime || reduceMotion()) {
    el.textContent = finalText;
    return;
  }
  const chars = "!<>-_\\/[]{}—=+*^?#";
  const len = finalText.length;
  let frame = 0;
  const total = Math.min(26, 10 + len);
  const timer = setInterval(() => {
    let out = "";
    for (let i = 0; i < len; i++) {
      const progress = frame / total - (i / len) * 0.4;
      out += progress > 0.7 ? finalText[i] : chars[Math.floor(Math.random() * chars.length)];
    }
    el.textContent = out;
    frame++;
    if (frame > total) {
      clearInterval(timer);
      el.textContent = finalText;
    }
  }, 20);
}

/* ---------- model brand: icon + accent ---------- */

const MODEL_BRANDS = [
  { re: /deepseek/i, icon: "deepseek", color: "#4d6bfe", name: "DeepSeek" },
  { re: /claude|anthropic/i, icon: "claude", color: "#d97757", name: "Claude" },
  { re: /gpt|openai|davinci|o[1-9]/i, icon: "openai", color: "#10a37f", name: "GPT" },
  { re: /gemini|bard/i, icon: "gemini", color: "#4285f4", name: "Gemini" },
  { re: /glm|chatglm|zhipu/i, icon: "glm", color: "#3856ff", name: "GLM" },
  { re: /command|cohere/i, icon: "cohere", color: "#39594d", name: "Cohere" },
  { re: /mistral/i, icon: "mistral", color: "#ff7000", name: "Mistral" },
  { re: /llama/i, icon: "llama", color: "#0467df", name: "Llama" },
  { re: /qwen/i, icon: "qwen", color: "#615ced", name: "Qwen" }
];

function detectBrand(id) {
  if (!id) return { icon: "fallback", color: "", name: "" };
  for (const b of MODEL_BRANDS) if (b.re.test(id)) return b;
  return { icon: "fallback", color: "", name: "" };
}

// Brand logos are PNG; only the generic fallback stayed an SVG.
function brandIconPath(brand) {
  return "vendor/model-icons/" + brand + (brand === "fallback" ? ".svg" : ".png");
}

function attachIconFallback(img) {
  img.onerror = () => {
    img.onerror = null;
    img.src = "vendor/model-icons/fallback.svg";
  };
}

function setAccent(hex) {
  const root = document.documentElement;
  if (!hex) return;
  // Set the accent instantly so it always applies, then pulse the brand mark.
  root.style.setProperty("--amber", hex);
  root.style.setProperty("--amber-deep", hex);
  if (agentAccent) {
    let value = hex.slice(1);
    if (value.length === 3) value = [...value].map(c => c + c).join("");
    const rgb = [0, 2, 4].map(i => parseInt(value.slice(i, i + 2), 16) / 255)
      .map(c => c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
    const luminance = 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2];
    root.style.setProperty("--agent-accent-fg", luminance > 0.179 ? "#141414" : "#ffffff");
  }
  if (window.anime && !reduceMotion()) {
    const pulse = document.querySelector(".brand .mark, #send");
    if (pulse) {
      anime({ targets: pulse, scale: [1, 1.12, 1], duration: 420, easing: "easeOutQuad" });
    }
  }
}

function paintBrand(id) {
  const b = detectBrand(id);
  const ico = document.querySelector("#modelpill .mico");
  if (ico) {
    const img = document.createElement("img");
    img.src = brandIconPath(b.icon);
    img.alt = "";
    img.title = b.name;
    attachIconFallback(img);
    ico.innerHTML = "";
    ico.appendChild(img);
    if (window.anime && !reduceMotion()) {
      anime({ targets: img, scale: [0.3, 1.25, 1], rotate: [10, 0], duration: 460, easing: "easeOutQuad" });
    }
  }
  setAccent(agentAccent || b.color);
}

function setActiveModel(id) {
  const name = $("modelname");
  if (name) name.textContent = shortModel(id);
  paintBrand(id);
  renderEffortPicker(id);
}

function renderEffortPicker(id) {
  const picker = $('effort-picker'), menu = $('effort-menu');
  const info = models.find(item => item.id === id);
  const knownLevels = new Set(['minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra']);
  const levels = info && info.reasoning && info.effortSupported && Array.isArray(info.efforts)
    ? [...new Set(info.efforts.filter(level => knownLevels.has(level)))] : [];
  menu.replaceChildren(); picker.open = false;
  picker.classList.toggle('hidden', !levels.length);
  selectedEffort = '';
  if (!levels.length) return;
  const key = 'beecode-effort:' + modelsProvider + ':' + id;
  const saved = localStorage.getItem(key) || '';
  selectedEffort = levels.includes(saved) ? saved : '';
  const choices = [{value:'', label:'Авто'}, ...levels.map(value => ({value, label:value}))];
  const update = () => {
    $('effort-label').textContent = selectedEffort || 'Авто';
    for (const button of menu.querySelectorAll('button')) {
      button.setAttribute('aria-selected', String(button.dataset.value === selectedEffort));
    }
  };
  for (const choice of choices) {
    const button = document.createElement('button');
    button.type = 'button'; button.dataset.value = choice.value; button.setAttribute('role', 'option');
    const text = document.createElement('span'); text.textContent = choice.label;
    const check = document.createElement('span'); check.className = 'effort-check'; check.textContent = '✓'; check.setAttribute('aria-hidden', 'true');
    button.append(text, check);
    button.onclick = () => { selectedEffort = choice.value; localStorage.setItem(key, selectedEffort); update(); picker.open = false; $('effort-trigger').focus(); };
    menu.append(button);
  }
  update();
}

$('effort-picker').addEventListener('toggle', () => {
  $('effort-trigger').setAttribute('aria-expanded', String($('effort-picker').open));
});
$('effort-picker').addEventListener('keydown', event => {
  const picker = $('effort-picker'), options = [...$('effort-menu').querySelectorAll('button')];
  if (event.key === 'Escape' && picker.open) {
    event.preventDefault(); picker.open = false; $('effort-trigger').focus();
  } else if (picker.open && ['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key) && options.length) {
    event.preventDefault();
    const index = options.indexOf(document.activeElement);
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? options.length - 1 :
      event.key === 'ArrowDown' ? (index + 1) % options.length : (index <= 0 ? options.length - 1 : index - 1);
    options[next].focus();
  }
});
document.addEventListener('click', event => {
  const picker = $('effort-picker');
  if (picker.open && !picker.contains(event.target)) picker.open = false;
});

/* ---------- composer ---------- */

function autogrow() {
  input.style.height = "auto";
  input.style.height = Math.min(190, input.scrollHeight) + "px";
}

async function boot() {
  try {
    const health = await api("/api/health");
    if (!health.ok) throw new Error("backend not ok");
  } catch (error) {
    return fatal("Нет связи с агентом (" + error.message + ").\nПерезапусти приложение.");
  }
  greet();
  let savedTheme = "paper";
  try {
    savedTheme = localStorage.getItem("beecode-theme") || "paper";
  } catch { /* private mode keeps paper */ }
  applyTheme(savedTheme);
  let savedSkin = "brutal";
  try {
    savedSkin = localStorage.getItem("beecode-skin") || "brutal";
  } catch { /* private mode keeps brutal */ }
  applySkin(savedSkin);
  const deep = /^#session=([A-Za-z0-9._-]+)$/.exec(location.hash || "");
  await identify();
  await loadConfig();
  await Promise.all([checkTrust(), loadModels(), refreshSessions()]);
  if (deep) {
    try { await switchSession(deep[1]); } catch { /* list stands */ }
  }
  paintBar(0, 0, 0);
  pollUiPrefs();
  setInterval(pollUiPrefs, 10000);
  $("send").onclick = sendMessage;
  $("stop").onclick = async () => {
    try { await post("/api/stop"); } catch { /* already done */ }
    if (streamAbort) streamAbort.abort();
  };
  $("attach").onclick = attachFiles;
  $("new-chat").onclick = newChat;
  $("search").oninput = () => drawSessions(sessionCache);
  $("gear").onclick = openSettings;
  document.querySelectorAll("#appearance-theme button").forEach((btn) => {
    btn.onclick = () => { applyTheme(btn.dataset.v); stampNow("beecode-theme-ts"); };
  });
  document.querySelectorAll("#appearance-skin button").forEach((btn) => {
    btn.onclick = () => { applySkin(btn.dataset.v); stampNow("beecode-skin-ts"); };
  });
  $("code-panel-close").onclick = closeCodePanel;
  wireCodeSelection();
  $("set-close").onclick = closeSettings;
  $("settings-back").addEventListener("click", (event) => {
    if (event.target.id === "settings-back") closeSettings();
  });
  document.addEventListener("keydown", (event) => {
    if (event.ctrlKey && event.shiftKey && event.key.toLowerCase() === "u") {
      event.preventDefault();
      resetAgentUi();
      return;
    }
    if (event.key === "Escape") closeSettings();
  });
  $("reset-agent-ui").onclick = resetAgentUi;
  document.querySelectorAll(".set-tabs button").forEach((btn) => {
    btn.onclick = () => switchTab(btn.dataset.tab);
  });
  document.querySelectorAll("#set-modes button").forEach((btn) => {
    btn.onclick = () => setMode(btn.dataset.mode);
  });
  document.querySelectorAll("#ag-mode, #ag-lang").forEach((seg) => {
    seg.querySelectorAll("button").forEach((btn) => {
      btn.onclick = () => segSet(seg.id, btn.dataset.v);
    });
  });
  $("ag-save").onclick = saveAgentPane;
  $("ins-save").onclick = saveInstructionsPane;
  $("ins-clear").onclick = clearInstructionsPane;
  $("ins-text").addEventListener("input", refreshInsMeta);
  $("ins-text").addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
      event.preventDefault();
      saveInstructionsPane();
    }
  });
  $("p-url-save").onclick = poolUrlSave;
  $("p-enroll").onclick = poolEnroll;
  $("p-google").onclick = poolGoogle;
  $("p-phone").onclick = pairPhone;
  $("p-use").onclick = poolUse;
  $("gate-google").onclick = gateGoogle;
  $("gate-skip").onclick = gateHide;
  await checkStartupSeat();
  $("f-save").onclick = saveProviderForm;
  $("f-add-model").onclick = () => addProviderModelRow();
  addProviderModelRow();
  const menu = $("modelmenu");
  $("modelpill").onclick = (event) => {
    event.stopPropagation();
    const opening = menu.classList.contains("hidden");
    menu.classList.toggle("hidden");
    if (opening && window.anime && !reduceMotion()) {
      anime({
        targets: menu.children,
        translateY: [10, 0],
        duration: 280,
        delay: anime.stagger(24),
        easing: "easeOutQuad"
      });
    }
  };
  document.addEventListener("click", (event) => {
    if (!menu.classList.contains("hidden") && !menu.contains(event.target)) {
      menu.classList.add("hidden");
    }
  });
  $("permmode").onclick = () => {
    const order = ["ask", "auto", "readonly"];
    setMode(order[(order.indexOf(permMode) + 1) % order.length]);
  };
  input.addEventListener("input", autogrow);
  input.addEventListener("input", slashHint);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      sendMessage();
    }
    if (event.key === "Escape") {
      const hint = $("slashmenu");
      if (hint) hint.classList.add("hidden");
    }
  });
  input.focus();
}

/* ---------- settings ---------- */

let providersCache = null;
let editingProviderName = "";

function openSettings() {
  $("settings-back").classList.remove("hidden");
  switchTab("providers");
  loadProviders();
  renderSettingsModels();
  loadAgentPane();
  loadInstructionsPane();
  paintFolderPane();
}

function closeSettings() {
  $("settings-back").classList.add("hidden");
}

function switchTab(name) {
  document.querySelectorAll(".set-tabs button").forEach((btn) => {
    btn.classList.toggle("on", btn.dataset.tab === name);
  });
  document.querySelectorAll("[data-pane]").forEach((pane) => {
    const show = pane.dataset.pane === name;
    pane.classList.toggle("hidden", !show);
    if (show && window.anime && !reduceMotion()) {
      anime({ targets: pane, translateY: [10, 0], duration: 260, easing: "easeOutQuad" });
    }
  });
  if (name === "pool") loadPoolPane();
  if (name === "extensions") loadExtensions();
}

async function loadExtensions() {
  const skills = $("extension-skills"), servers = $("extension-servers");
  try {
    const data = await api("/api/extensions");
    skills.replaceChildren();
    servers.replaceChildren();
    if (!data.skills.length) skills.textContent = "Навыки не установлены";
    for (const skill of data.skills) {
      const row = document.createElement("div");
      row.className = "kv";
      const title = document.createElement("b"), description = document.createElement("span");
      title.textContent = skill.name;
      if (skill.builtin) {
        const badge = document.createElement('span'); badge.className = 'builtin-skill-badge'; badge.textContent = 'Встроен';
        title.append(' ', badge);
      }
      description.textContent = skill.description;
      row.append(title, description);
      skills.append(row);
    }
    if (!data.servers.length) servers.textContent = "Серверы не настроены";
    for (const server of data.servers) {
      const row = document.createElement("div");
      row.className = "kv";
      const title = document.createElement("b"), status = document.createElement("span");
      title.textContent = server.builtin ? "customuze beecode" : server.name;
      status.textContent = server.builtin ? (server.enabled ? `Встроен · инструментов: ${server.tools}` : "Не удалось загрузить") : !server.enabled ? "Выключен" : server.withheld ? "Нет доверия к папке" :
        server.discovered ? `Инструментов: ${server.tools}` : "Не подключён";
      row.append(title, status);
      if (!server.builtin && server.enabled && !server.withheld) {
        const connect = document.createElement("button");
        connect.textContent = server.discovered ? "Обновить" : "Подключить";
        connect.onclick = async () => {
          connect.disabled = true;
          $("extension-message").textContent = "Подключение…";
          try {
            const result = await post("/api/extensions/connect", { name: server.name });
            $("extension-message").textContent = `Подключено инструментов: ${result.tools}`;
            await loadExtensions();
          } catch (error) {
            $("extension-message").textContent = error.message || "Не удалось подключить сервер";
          } finally { connect.disabled = false; }
        };
        row.append(connect);
      }
      servers.append(row);
    }
  } catch (error) {
    $("extension-message").textContent = error.message || "Не удалось загрузить расширения";
  }
}

function esc(text) {
  const div = document.createElement("div");
  div.textContent = String(text == null ? "" : text);
  return div.innerHTML;
}

async function loadProviders() {
  const list = $("prov-list"), presets = $("preset-list");
  try {
    const body = await api("/api/providers");
    providersCache = body;
    list.innerHTML = "";
    if (!body.providers || !body.providers.length) {
      list.innerHTML = '<div class="set-empty">Ничего не настроено.</div>';
    }
    for (const p of body.providers || []) {
      const row = document.createElement("div");
      row.className = "prov" + (p.active ? " active" : "");
      const top = document.createElement("div");
      top.className = "prov-top";
      const name = document.createElement("b");
      name.textContent = p.name + (p.active ? " ←" : "");
      const kind = document.createElement("span");
      kind.className = "prov-kind";
      kind.textContent = p.kind === "custom" ? (p.url || "endpoint")
        : (p.keyless ? "без ключа" : "встроен");
      const key = document.createElement("span");
      key.className = "prov-key";
      key.textContent = p.hasKey ? ("ключ " + (p.keyTail || "")) : "";
      const btns = document.createElement("span");
      btns.className = "prov-btns";
      if (!p.active) {
        const go = document.createElement("button");
        go.className = "go";
        go.textContent = "Включить";
        go.onclick = () => useProvider(p.name);
        btns.append(go);
      }
      if (p.kind === "custom") {
        const edit = document.createElement("button");
        edit.textContent = "Править";
        edit.onclick = () => {
          editingProviderName = p.name;
          $("f-name").value = p.name;
          $("f-url").value = p.url || "";
          setProviderModelRows(p.modelDetails && p.modelDetails.length ? p.modelDetails : (p.models || []).map(id => ({id})));
          $("f-keys").value = "";
          setFormMsg("Ключи сохранятся; введи новые, чтобы заменить.", false);
          $("f-keys").focus();
        };
        const del = document.createElement("button");
        del.className = "danger";
        del.textContent = "Удалить";
        del.onclick = () => removeProvider(p.name);
        btns.append(edit, del);
      }
      top.append(name, kind, key, btns);
      row.append(top);
      if (p.models && p.models.length) {
        const m = document.createElement("div");
        m.className = "prov-models";
        m.textContent = p.models.slice(0, 4).join(", ")
          + (p.models.length > 4 ? `  +${p.models.length - 4}` : "");
        row.append(m);
      }
      list.append(row);
    }
    presets.innerHTML = "";
    for (const e of body.presets || []) {
      const row = document.createElement("div");
      row.className = "prov" + (e.active ? " active" : "");
      const top = document.createElement("div");
      top.className = "prov-top";
      const name = document.createElement("b");
      name.textContent = e.label + (e.active ? " ←" : "");
      const key = document.createElement("span");
      key.className = "prov-key";
      key.textContent = e.hasKey ? ("ключ " + (e.keyTail || ""))
        : (e.keyless ? "отвечает и без ключа" : "нужен ключ");
      const btns = document.createElement("span");
      btns.className = "prov-btns";
      const fill = document.createElement("button");
      fill.textContent = e.hasKey ? "Заменить ключ" : "Вставить ключ";
      fill.onclick = () => {
        editingProviderName = "";
        $("f-name").value = e.name;
        $("f-url").value = "";
        setProviderModelRows([]);
        $("f-keys").value = "";
        setFormMsg(`Ключ для ${e.name} — взять: ${e.signup}`, false);
        $("f-keys").focus();
      };
      btns.append(fill);
      top.append(name, key, btns);
      row.append(top);
      const sub = document.createElement("div");
      sub.className = "prov-signup";
      sub.innerHTML = `${esc(e.free)} · <a href="${esc(e.signup)}" target="_blank" rel="noreferrer">${esc(e.signup)}</a>`;
      for (const a of sub.querySelectorAll("a")) {
        a.onclick = (ev) => ev.stopPropagation();
      }
      row.append(sub);
      presets.append(row);
    }
  } catch (error) {
    list.innerHTML = '<div class="set-empty">⚠ ' + esc(String(error).slice(0, 160)) + "</div>";
  }
}

async function useProvider(name) {
  try {
    const body = await post("/api/providers", { action: "use", name });
    if (body.model) {
      model = body.model;
      setActiveModel(model);
    }
    providersCache = body.providers || providersCache;
    await Promise.all([loadConfig(), loadModels()]);
    loadProviders();
  } catch (error) {
    setFormMsg(String(error).slice(0, 200), true);
  }
}

async function removeProvider(name) {
  try {
    const body = await post("/api/providers", { action: "remove", name });
    setFormMsg(body.message || "Удалено.", false);
    loadProviders();
  } catch (error) {
    setFormMsg(String(error).slice(0, 200), true);
  }
}

function setFormMsg(text, isErr) {
  const box = $("f-msg");
  box.textContent = text || "";
  box.classList.toggle("err", !!isErr);
  box.classList.toggle("ok", !!text && !isErr);
}

function addProviderModelRow(item = {}) {
  const row = document.createElement("div");
  row.className = "provider-model-row";
  row.innerHTML = '<label>Название<input data-field="name" placeholder="Название модели"></label>' +
    '<label>ID модели<input data-field="id" placeholder="provider/model-id" required></label>' +
    '<label>Контекст, токены<input data-field="context" type="number" min="1024" max="10000000" placeholder="Неизвестен"></label>' +
    '<div class="reasoning-control"><label class="reasoning-toggle"><input data-field="reasoning" type="checkbox" aria-label="Поддерживает reasoning"><span>Поддерживает reasoning</span></label>' +
    '<details class="model-efforts" hidden><summary>Выбрать уровни</summary><div class="model-effort-popover"><strong>Уровни рассуждения</strong>' +
    '<div class="model-effort-options">' + ["minimal", "low", "medium", "high", "xhigh", "max", "ultra"].map(level =>
      `<label><input type="checkbox" data-effort="${level}"><span>${level}</span></label>`).join("") +
    '</div><small>Выбери поддерживаемые уровни. Если они неизвестны, оставь пустым.</small></div></details></div>' +
    '<button type="button" data-remove>Убрать</button>';
  row.querySelector('[data-field="name"]').value = item.name || "";
  row.querySelector('[data-field="id"]').value = item.id || "";
  row.querySelector('[data-field="context"]').value = item.context || "";
  const reasoning = row.querySelector('[data-field="reasoning"]');
  const picker = row.querySelector('.model-efforts');
  const updatePicker = () => {
    const selected = [...row.querySelectorAll('[data-effort]:checked')].map(input => input.dataset.effort);
    picker.querySelector('summary').textContent = selected.length ? selected.join(', ') : 'Выбрать уровни';
    picker.querySelector('summary').title = selected.length ? selected.join(', ') : 'Выбрать поддерживаемые уровни';
  };
  reasoning.checked = !!item.reasoning;
  picker.hidden = !reasoning.checked;
  row.querySelector('.reasoning-toggle span').hidden = reasoning.checked;
  reasoning.onchange = () => { picker.hidden = !reasoning.checked; picker.open = false;
    row.querySelector('.reasoning-toggle span').hidden = reasoning.checked; };
  for (const checkbox of row.querySelectorAll('[data-effort]')) {
    checkbox.checked = Array.isArray(item.efforts) && item.efforts.includes(checkbox.dataset.effort);
    checkbox.onchange = updatePicker;
  }
  updatePicker();
  picker.addEventListener('toggle', () => {
    if (!picker.open) return;
    picker.classList.remove('opens-up');
    const popover = picker.querySelector('.model-effort-popover');
    const bounds = $('settings').getBoundingClientRect();
    if (popover.getBoundingClientRect().bottom > bounds.bottom - 12) picker.classList.add('opens-up');
  });
  picker.addEventListener('keydown', event => { if (event.key === 'Escape') { picker.open = false; picker.querySelector('summary').focus(); } });
  picker.addEventListener('focusout', () => setTimeout(() => { if (!picker.contains(document.activeElement)) picker.open = false; }, 0));
  row.querySelector('[data-remove]').onclick = () => row.remove();
  $("f-model-rows").append(row);
}

function setProviderModelRows(items) {
  $("f-model-rows").replaceChildren();
  (items.length ? items : [{}]).forEach(addProviderModelRow);
}

async function saveProviderForm() {
  const details = [...$("f-model-rows").children].map((row) => ({
    id: row.querySelector('[data-field="id"]').value.trim(),
    name: row.querySelector('[data-field="name"]').value.trim(),
    context: row.querySelector('[data-field="context"]').value ? Number(row.querySelector('[data-field="context"]').value) : null,
    reasoning: row.querySelector('[data-field="reasoning"]').checked,
    efforts: row.querySelector('[data-field="reasoning"]').checked
      ? [...row.querySelectorAll('[data-effort]:checked')].map(input => input.dataset.effort) : [],
  })).filter((item) => item.id || item.name || item.context || item.reasoning);
  const payload = {
    action: "save",
    name: $("f-name").value.trim(),
    url: $("f-url").value.trim(),
    keys: $("f-keys").value.trim(),
    models: details.map((item) => item.id),
    modelDetails: details,
    keepKeys: editingProviderName === $("f-name").value.trim() && !$('f-keys').value.trim(),
  };
  if (!payload.name) {
    setFormMsg("Назови провайдера.", true);
    return;
  }
  setFormMsg("Сохраняю…", false);
  try {
    const body = await post("/api/providers", payload);
    setFormMsg(body.message || "Сохранено.", false);
    $("f-keys").value = "";
    await Promise.all([loadConfig(), loadModels()]);
    loadProviders();
  } catch (error) {
    setFormMsg(String(error).slice(0, 220), true);
  }
}

function renderSettingsModels() {
  const box = $("set-models");
  box.innerHTML = "";
  if (!models.length) {
    box.innerHTML = '<div class="set-empty">Нет моделей. Проверь провайдера.</div>';
    return;
  }
  for (const item of models) {
    const row = document.createElement("div");
    row.className = "mi" + (item.id === model ? " on" : "");
    const ico = document.createElement("img");
    ico.src = brandIconPath(detectBrand(item.id).icon);
    ico.alt = "";
    attachIconFallback(ico);
    const name = document.createElement("span");
    name.textContent = item.name || item.id;
    const meta = document.createElement("small");
    meta.textContent = `окно ${item.measured ? "✔ " : "~"}${fmtWindow(item.window)}${item.reasoning ? " · reasoning" : ""}`;
    meta.title = "Полное окно модели; приблизительное значение может быть непроверенным. Лимит входа BeeCode показан на полосе контекста и может быть меньше.";
    row.append(ico, name, meta);
    row.onclick = () => {
      model = item.id;
      setActiveModel(item.id);
      [...box.children].forEach((el) => el.classList.remove("on"));
      row.classList.add("on");
      const menu = $("modelmenu");
      [...menu.children].forEach((el) => {
        el.classList.toggle("on", el.textContent.includes(item.id));
      });
      post("/api/config", { model: item.id }).catch(() => {});
    };
    box.append(row);
  }
}

function segVal(id) {
  const on = document.querySelector(`#${id} button.on`);
  return on ? on.dataset.v : null;
}

function segSet(id, val) {
  document.querySelectorAll(`#${id} button`).forEach((btn) => {
    btn.classList.toggle("on", btn.dataset.v === val);
  });
}

async function loadAgentPane() {
  try {
    const body = await api("/api/config");
    segSet("ag-mode", body.mode === "economy" ? "economy" : "normal");
    segSet("ag-lang", body.language === "en" ? "en" : "ru");
    $("ag-native").checked = body.nativeTools !== false;
    $("ag-turns").value = body.maxTurns != null ? body.maxTurns : 50;
    $("ag-ctx").value = body.maxContextTokens != null ? body.maxContextTokens : 0;
    $("ag-idle").value = body.streamIdleTimeout != null ? body.streamIdleTimeout : 90;
    $("ag-cache").checked = body.cacheEnabled !== false;
    $("ag-ttl").value = body.cacheTtlMinutes != null ? body.cacheTtlMinutes : 30;
  } catch { /* pane keeps defaults */ }
}

async function saveAgentPane() {
  const msg = $("ag-msg");
  const num = (id) => {
    const v = $(id).value.trim();
    return v === "" ? null : Number(v);
  };
  const payload = {
    mode: segVal("ag-mode") || "normal",
    language: segVal("ag-lang") || "ru",
    nativeTools: $("ag-native").checked,
    cacheEnabled: $("ag-cache").checked,
  };
  const turns = num("ag-turns"), ctx = num("ag-ctx"),
    idle = num("ag-idle"), ttl = num("ag-ttl");
  if (turns !== null) payload.maxTurns = turns;
  if (ctx !== null) payload.maxContextTokens = ctx;
  if (idle !== null) payload.streamIdleTimeout = idle;
  if (ttl !== null) payload.cacheTtlMinutes = ttl;
  msg.textContent = "Сохраняю…";
  msg.classList.remove("err");
  msg.classList.add("ok");
  try {
    const body = await post("/api/config", payload);
    msg.textContent = "Сохранено: " + (body.changed || []).join(", ");
  } catch (error) {
    msg.textContent = String(error).slice(0, 220);
    msg.classList.remove("ok");
    msg.classList.add("err");
  }
}

/* ---------- instructions tab ---------- */

const INS_LIMIT = 8000;

function refreshInsMeta() {
  const text = $("ins-text").value || "";
  const badge = $("ins-badge");
  const count = $("ins-count");
  const empty = text.length === 0;
  badge.textContent = empty ? "Пусто" : "Активно";
  badge.classList.toggle("on", !empty);
  count.textContent = text.length.toLocaleString("ru") + " симв.";
  count.classList.toggle("warn", text.length > INS_LIMIT * 0.9);
  count.classList.toggle("limit", text.length > INS_LIMIT);
  $("ins-save").disabled = false;
}

async function loadInstructionsPane() {
  try {
    const body = await api("/api/config");
    $("ins-text").value = body.customInstructions || "";
    refreshInsMeta();
  } catch { /* pane keeps defaults */ }
}

async function saveInstructionsPane() {
  const msg = $("ins-msg");
  const text = $("ins-text").value;
  msg.textContent = "Сохраняю…";
  msg.classList.remove("err");
  msg.classList.add("ok");
  try {
    await post("/api/config", { customInstructions: text });
    msg.textContent = "Сохранено";
    msg.classList.remove("err");
    refreshInsMeta();
  } catch (error) {
    msg.textContent = String(error).slice(0, 220);
    msg.classList.remove("ok");
    msg.classList.add("err");
  }
}

async function clearInstructionsPane() {
  $("ins-text").value = "";
  refreshInsMeta();
  await saveInstructionsPane();
}

/* ---------- pool tab ---------- */

function poolMsg(text, isErr) {
  const box = $("p-msg");
  box.textContent = text || "";
  box.classList.toggle("err", !!isErr);
  box.classList.toggle("ok", !!text && !isErr);
}

function paintPool(data) {
  const dot = $("pool-dot");
  dot.classList.remove("ok", "bad");
  if (!data.url) {
    $("pool-state").textContent = "Адрес пула не задан — впиши его ниже.";
    dot.classList.add("bad");
  } else if (data.server && data.server.reachable) {
    $("pool-state").textContent = "Сервер: " + (data.server.detail || "отвечает");
    dot.classList.add("ok");
  } else {
    $("pool-state").textContent = "Сервер: " + ((data.server && data.server.detail) || "не отвечает");
    dot.classList.add("bad");
  }
  // The seat the pool signed up through Google is a verified identity: when
  // it reads approved, the green check appears next to the pool dot.
  const g = $("gverify");
  if (g) {
    const verified = !!(data.hasSeat && data.seat && data.seat.approved);
    const was = !g.classList.contains("hidden");
    g.classList.toggle("hidden", !verified);
    if (verified && !was && window.anime && !reduceMotion()) {
      g.classList.add("pop");
      setTimeout(() => g.classList.remove("pop"), 500);
    }
  }
  $("pool-url").textContent = data.url || "—";
  $("pool-seat").textContent = data.hasSeat ? ("есть " + (data.seatTail || "")) : "нет — возьми кнопкой ниже";
  const seat = data.seat || {};
  const left = seat.requests_left != null ? `${seat.requests_left} запр.` : null;
  const toks = seat.tokens_left != null ? `${seat.tokens_left} ток.` : null;
  const per = (seat.requests_per_day != null || seat.tokens_per_day != null)
    ? ` (в сутки: ${seat.requests_per_day != null ? seat.requests_per_day : "?"}, ${seat.tokens_per_day != null ? seat.tokens_per_day : "?"})` : "";
  $("pool-budget").textContent = (left || toks) ? ((left ? left : "") + (left && toks ? " · " : "") + (toks ? toks : "") + per) : "—";
  if (!$("p-url").value) $("p-url").value = data.url || "";
}

async function loadPoolPane() {
  $("pool-state").textContent = "Опрашиваю пул… (бесплатный сервер может просыпаться до минуты)";
  $("pool-dot").classList.remove("ok", "bad");
  try {
    const body = await api("/api/pool");
    paintPool(body);
  } catch (error) {
    $("pool-state").textContent = "⚠ " + String(error).slice(0, 160);
    $("pool-dot").classList.add("bad");
  }
}

async function poolUrlSave() {
  const url = $("p-url").value.trim();
  if (!url) { poolMsg("Впиши адрес пула.", true); return; }
  poolMsg("Сохраняю…", false);
  try {
    const body = await post("/api/pool", { action: "url", url });
    poolMsg(body.message || "Сохранено.", false);
    if (body.pool) paintPool(body.pool);
  } catch (error) {
    poolMsg(String(error).slice(0, 220), true);
  }
}

async function poolEnroll() {
  poolMsg("Прошу место у пула… (холодный сервер просыпается до минуты)", false);
  try {
    const body = await post("/api/pool", {
      action: "enroll", secret: $("p-secret").value.trim(),
    });
    poolMsg(body.message || "Место получено.", false);
    $("p-secret").value = "";
    if (body.pool) paintPool(body.pool);
    else loadPoolPane();
  } catch (error) {
    poolMsg(String(error).slice(0, 240), true);
  }
}

let googlePending = false;
const GOOGLE_WAIT_MS = 180000;

async function googlePost(action, remaining) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), Math.min(20000, remaining));
  try {
    return await api("/api/pool", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action }), signal: controller.signal,
    });
  } finally { clearTimeout(timer); }
}

function celebrateSeat(tail) {
  // The one moment this install becomes a someone: the seat read approved
  // for the first time in this sign-in flow. Banner (pure CSS, plays even
  // without the motion libraries) plus confetti where allowed.
  const banner = $("p-success");
  if (!banner) return;
  if (tail) banner.textContent = "🎉 Вы зарегистрировались! Место …" + tail + " привязано к этой машине.";
  banner.classList.remove("hidden");
  banner.classList.add("show");
  try { if (typeof popIn === "function") popIn(banner); } catch { /* decoration only */ }
  try { if (typeof celebrate === "function") celebrate(); } catch { /* decoration only */ }
  setTimeout(() => { banner.classList.add("hidden"); banner.classList.remove("show"); }, 8000);
}

// Startup gate: no Google-verified seat, no pool. The gate is UX only —
// the server itself refuses unverified installs with 403, so removing this
// overlay from the client buys nothing: the pool still will not answer.
let gateOpen = false;

function gateShow() {
  gateOpen = true;
  const g = $("gate");
  if (g) g.classList.remove("hidden");
}

function gateHide() {
  gateOpen = false;
  const g = $("gate");
  if (g) g.classList.add("hidden");
}

async function checkStartupSeat() {
  // Every launch until registered. Offline keeps the gate up rather than
  // letting the pool fail later with a cryptic error.
  let verified = false;
  try {
    const body = await api("/api/pool");
    verified = !!(body && body.hasSeat && body.seat && body.seat.approved);
  } catch { /* the gate stays, the message below explains nothing yet */ }
  if (!verified) gateShow();
}

async function gateGoogle() {
  // The same waiting flow as the pool tab: the gate closes only when the
  // server itself reads the seat approved.
  const msg = $("gate-msg");
  if (msg) { msg.textContent = "Открываю вход через Google…"; msg.classList.remove("err"); }
  if (await poolGoogle()) gateHide();
  else if (msg) { msg.textContent = "Не получилось — попробуй ещё раз или продолжи без пула."; msg.classList.add("err"); }
}

async function poolGoogle() {
  if (googlePending) return;
  googlePending = true;
  const controls = ["p-google", "p-enroll", "p-url-save"];
  controls.forEach(id => { $(id).disabled = true; });
  poolMsg("Открываю вход через Google…", false);
  try {
    const start = await googlePost("google", 20000);
    if (!window.beeDesk || !window.beeDesk.openExternal ||
        !await window.beeDesk.openExternal(start.url)) {
      throw new Error("Не удалось открыть браузер. Повтори вход через Google.");
    }
    const deadline = Date.now() + GOOGLE_WAIT_MS;
    let detail = "Заверши вход в браузере. Ожидаю место…";
    // Celebrate only a seat this flow actually waited for: an install that
    // is already registered reads complete on the first poll, and greeting
    // it as newly registered would be a lie.
    let waited = false;
    while (Date.now() < deadline) {
      poolMsg(detail, false);
      try {
        const status = await googlePost("google-status", deadline - Date.now());
        if (status.complete) {
          if (status.pool) paintPool(status.pool);
          poolMsg(status.message || "Место получено. Включи провайдер pool кнопкой ниже.", false);
          if (waited) celebrateSeat(status.pool && status.pool.seatTail);
          return true;
        }
        detail = status.message || "Заверши вход в браузере. Ожидаю место…";
        waited = true;
      } catch (error) {
        if (error.body && error.status !== 502 && error.status !== 503 && error.status !== 504) throw error;
        detail = "Пул временно недоступен, повторяю проверку…";
        waited = true;
      }
      await new Promise(done => setTimeout(done, Math.min(2000, Math.max(0, deadline - Date.now()))));
    }
    throw new Error("Время ожидания истекло. Проверь результат в браузере и адрес пула. Повтори вход через Google.");
  } catch (error) {
    poolMsg(String(error.message || error).slice(0, 240), true);
  } finally {
    googlePending = false;
    controls.forEach(id => { $(id).disabled = false; });
  }
  return false;
}

let phonePoll = null;

async function pairPhone() {
  // One live channel at a time: pressing again while one is open returns the
  // same code the server is already listening on, so this just re-shows it.
  poolMsg("Открываю канал для телефона…", false);
  try {
    const body = await post("/api/pair", { action: "start" });
    const code = String(body.code || "").trim();
    if (!code) {
      poolMsg(body.error || "Пул не дал код — попробуй ещё раз.", true);
      return;
    }
    const box = $("p-phone-code");
    box.textContent = code;
    box.classList.remove("hidden");
    if (box.classList.contains("pop")) box.classList.remove("pop");
    setTimeout(() => box.classList.add("pop"), 20);
    poolMsg("Введи код в приложении за 5 минут.", false);
    if (phonePoll) clearInterval(phonePoll);
    phonePoll = setInterval(async () => {
      try {
        const st = await api("/api/pair");
        if (st && st.paired) {
          clearInterval(phonePoll);
          phonePoll = null;
          poolMsg("Телефон подключён — пиши с него, всё воркает здесь.", false);
          setTimeout(() => { box.classList.add("hidden"); }, 4000);
        }
      } catch { /* the pair window outlives a hiccup; keep waiting */ }
    }, 2000);
  } catch (error) {
    poolMsg(String(error).slice(0, 240), true);
  }
}

async function poolUse() {
  poolMsg("Включаю провайдер pool…", false);
  try {
    const body = await post("/api/providers", { action: "use", name: "pool" });
    if (body.model) {
      model = body.model;
      setActiveModel(model);
    }
    await Promise.all([loadConfig(), loadModels()]);
    poolMsg("Провайдер pool включён, модель: " + (body.model || "—"), false);
  } catch (error) {
    poolMsg(String(error).slice(0, 240), true);
  }
}

async function paintFolderPane() {  try {
    const body = await api("/api/config");
    $("set-workdir").textContent = body.workdir || workdir || "…";
  } catch {
    $("set-workdir").textContent = workdir || "…";
  }
  try {
    const trust = await api("/api/trust");
    $("set-trust").textContent = trust.trusted ? "доверенная ✓"
      : ("не доверена" + (((trust.claims || []).length)
        ? ` — требований: ${trust.claims.length}` : ""));
  } catch {
    $("set-trust").textContent = "…";
  }
}

boot();
