/* BeeCode design variants — shared demo engine.
 * Builds the same chat UI DOM for every skin, then animates it with anime.js.
 * Skins only provide CSS; this file owns structure + motion + the model
 * picker that swaps provider icon and the --accent color. */

(function () {
  "use strict";

  var ICON_DIR = "../vendor/model-icons/";

  var MODELS = [
    { id: "deepseek-v4-pro", brand: "deepseek", name: "DeepSeek", color: "#4d6bfe" },
    { id: "gpt-6-astra",     brand: "openai",   name: "GPT",      color: "#10a37f" },
    { id: "claude-opus-4",   brand: "claude",   name: "Claude",   color: "#d97757" },
    { id: "gemini-2.5-pro",  brand: "gemini",   name: "Gemini",   color: "#4285f4" },
    { id: "glm-5.3-flash",   brand: "glm",      name: "GLM",      color: "#3856ff" },
    { id: "command-r-plus",  brand: "cohere",   name: "Cohere",   color: "#39594d" },
    { id: "mistral-large",   brand: "mistral",  name: "Mistral",  color: "#ff7000" },
    { id: "llama-4-scout",   brand: "llama",    name: "Llama",    color: "#0467df" },
    { id: "qwen-3-max",      brand: "qwen",     name: "Qwen",     color: "#615ced" }
  ];

  var SESSIONS = [
    "Сайт для пекарни", "Рефакторинг модуля", "Парсер логов", "Промпт для бота",
    "Разбор багов", "Идеи для поста", "Калькулятор на JS"
  ];

  var active = MODELS[0];

  function el(tag, cls, html) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html != null) n.innerHTML = html;
    return n;
  }

  function iconFor(brand) {
    return ICON_DIR + brand + (brand === "fallback" ? ".svg" : ".png");
  }

  function buildSidebar() {
    var side = el("aside", "sidebar");
    side.id = "sidebar";
    side.appendChild(el("div", "brand",
      '<span class="mark">B</span><span class="word">BeeCode</span>' +
      '<span class="ver">v10.0.9</span>'));
    side.appendChild(el("button", "new-chat", '<svg viewBox="0 0 16 16" width="14" height="14"><path d="M8 3v10M3 8h10" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>Новый чат'));
    side.appendChild(el("div", "search-row",
      '<svg viewBox="0 0 16 16" width="13" height="13"><circle cx="7" cy="7" r="4.5" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M10.5 10.5 14 14" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>' +
      '<input id="search" placeholder="Поиск по чатам" autocomplete="off">'));
    side.appendChild(el("div", "side-label", "Чаты"));
    var list = el("div", "sessions");
    list.id = "sessions";
    SESSIONS.forEach(function (s, i) {
      var row = el("div", "session" + (i === 0 ? " on" : ""),
        '<b>' + s + '</b><small>' + (i + 1) + " день назад</small>");
      list.appendChild(row);
    });
    side.appendChild(list);
    var bottom = el("div", "side-bottom");
    bottom.appendChild(el("div", "project", "BeeCode"));
    bottom.appendChild(el("div", "me",
      '<span class="avatar">А</span><span class="whoami">Администратор</span>' +
      '<button class="iconbtn" title="Сменить тему" id="theme">☾</button>' +
      '<button class="iconbtn" title="Настройки" id="gear"><svg viewBox="0 0 16 16" width="15" height="15"><circle cx="8" cy="8" r="2.2" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M8 1.8v2M8 12.2v2M1.8 8h2M12.2 8h2M3.6 3.6l1.4 1.4M11 11l1.4 1.4M12.4 3.6 11 5M5 11l-1.4 1.4" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg></button>'));
    side.appendChild(bottom);
    return side;
  }

  function chatMsg(role, text) {
    return el("div", "msg " + role, '<div class="bubble">' + text + "</div>");
  }

  function buildMain() {
    var main = el("main", "main");
    main.id = "main";
    var greet = el("div", "greet",
      "<h1 id='greet-text'>Добрый вечер.</h1>" +
      '<div class="hint" id="greet-sub">Спроси что-нибудь — напиши, что нужно сделать.</div>');
    main.appendChild(greet);
    var chat = el("div", "chat");
    chat.id = "chat";
    chat.appendChild(chatMsg("user", "Сделай красивую страницу для пекарни"));
    chat.appendChild(chatMsg("assistant",
      "Готово — собрал одностраничник с тёплой палитрой, витриной выпечки и формой заказа. Код в <code>index.html</code>, стили в <code>styles.css</code>. Открыл в браузере, вот результат."));
    main.appendChild(chat);

    var wrap = el("div", "composer-wrap");
    var chips = el("div", "chips");
    chips.appendChild(el("span", "chip", "выпечка"));
    chips.appendChild(el("span", "chip", "тёплая палитра"));
    wrap.appendChild(chips);

    var composer = el("div", "composer");
    var bar = el("div", "composer-bar");
    var left = el("div", "left");
    left.appendChild(el("button", "iconbtn attach", "@"));
    var pill = el("button", "modelpill");
    pill.id = "modelpill";
    pill.appendChild(el("span", "mico"));
    pill.appendChild(el("span", "modelname", active.name));
    pill.appendChild(el("span", "chev", "▾"));
    left.appendChild(pill);
    left.appendChild(el("button", "permmode", "Авто"));
    bar.appendChild(left);
    var send = el("button", "send", '<svg viewBox="0 0 16 16" width="15" height="15"><path d="M8 13V3M3.5 7.5 8 3l4.5 4.5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>');
    bar.appendChild(send);
    var ta = document.createElement("textarea");
    ta.className = "input";
    ta.rows = 1;
    ta.placeholder = "Спроси что-нибудь…  @ — файл, / — команда, Enter — отправить";
    composer.appendChild(ta);
    composer.appendChild(bar);
    var menu = el("div", "modelmenu hidden");
    menu.id = "modelmenu";
    MODELS.forEach(function (m) {
      var row = el("div", "mi" + (m === active ? " on" : ""),
        '<img src="' + iconFor(m.brand) + '" alt="">' +
        '<span class="mi-name">' + m.name + "</span>" +
        '<small>' + m.id + "</small>");
      row.dataset.brand = m.brand;
      row.dataset.color = m.color;
      row.addEventListener("click", function () { selectModel(m); });
      menu.appendChild(row);
    });
    composer.appendChild(menu);
    wrap.appendChild(composer);
    wrap.appendChild(el("div", "ctxbar", '<div class="ctxfill"><div></div></div><span>128k контекст</span>'));
    main.appendChild(wrap);
    return main;
  }

  function buildModelpillIcon(brand) {
    var img = document.createElement("img");
    img.src = iconFor(brand);
    img.alt = "";
    return img;
  }

  function selectModel(m) {
    active = m;
    var pill = document.getElementById("modelpill");
    var ico = pill.querySelector(".mico");
    ico.innerHTML = "";
    ico.appendChild(buildModelpillIcon(m.brand));
    pill.querySelector(".modelname").textContent = m.name;
    document.getElementById("modelmenu").classList.add("hidden");
    document.querySelectorAll(".mi").forEach(function (r) {
      r.classList.toggle("on", r.dataset.brand === m.brand);
    });
    animateAccent(m.color);
    pop(ico.firstChild);
  }

  function animateAccent(hex) {
    var root = document.documentElement;
    var end = { "--accent": hex, "--accent-soft": hex + "22" };
    anime({
      targets: end,
      duration: 700,
      easing: "easeOutCubic",
      update: function () {
        root.style.setProperty("--accent", end["--accent"]);
        root.style.setProperty("--accent-soft", end["--accent-soft"]);
      }
    });
  }

  function pop(node) {
    anime({
      targets: node,
      scale: [0.4, 1.2, 1],
      duration: 500,
      easing: "easeOutBack"
    });
  }

  function bindPill() {
    var pill = document.getElementById("modelpill");
    var menu = document.getElementById("modelmenu");
    pill.addEventListener("click", function (e) {
      e.stopPropagation();
      menu.classList.toggle("hidden");
      if (!menu.classList.contains("hidden")) {
        anime({
          targets: menu.children,
          opacity: [0, 1],
          translateY: [8, 0],
          scale: [0.97, 1],
          duration: 320,
          delay: anime.stagger(28),
          easing: "easeOutQuad"
        });
      }
    });
    document.addEventListener("click", function () {
      menu.classList.add("hidden");
    });
    var input = document.querySelector(".input");
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        simulateSend();
      }
    });
  }

  function simulateSend() {
    var input = document.querySelector(".input");
    var text = input.value.trim();
    if (!text) return;
    input.value = "";
    var chat = document.getElementById("chat");
    var greet = document.querySelector(".greet");
    if (greet) greet.style.display = "none";
    var user = chatMsg("user", text);
    chat.appendChild(user);
    animateIn([user], true);
    var ok = chatMsg("assistant", "Отличный вопрос! Уже думаю над «<b>" +
      text + "</b>». Выбери другой вариант оформления — у каждого свой характер.");
    setTimeout(function () {
      chat.appendChild(ok);
      animateIn([ok], false);
      chat.scrollTop = chat.scrollHeight;
    }, 650);
  }

  function animateIn(nodes, fromRight) {
    anime({
      targets: nodes,
      opacity: [0, 1],
      translateX: [fromRight ? 22 : -22, 0],
      translateY: [10, 0],
      duration: 520,
      easing: "easeOutCubic"
    });
  }

  function bootIntro() {
    var side = document.getElementById("sidebar");
    var main = document.getElementById("main");
    side.style.opacity = 0;
    main.style.opacity = 0;
    anime({
      targets: [".brand", ".new-chat", ".search-row", ".side-label", ".session", ".side-bottom"],
      opacity: [0, 1],
      translateX: [-16, 0],
      duration: 500,
      delay: anime.stagger(50),
      easing: "easeOutQuad",
      complete: function () { side.style.opacity = 1; }
    });
    anime({
      targets: [".greet", ".msg", ".composer-wrap"],
      opacity: [0, 1],
      translateY: [18, 0],
      duration: 600,
      delay: anime.stagger(140),
      easing: "easeOutCubic",
      complete: function () { main.style.opacity = 1; }
    });
    var comet = el("div", "comet");
    document.body.appendChild(comet);
    anime({
      targets: comet,
      translateX: ["-10vw", "110vw"],
      opacity: [0, 1, 0],
      duration: 1800,
      delay: 900,
      easing: "easeInOutQuad",
      complete: function () { comet.remove(); }
    });
    animateAccent(active.color);
  }

  function build() {
    var app = el("div", "app");
    app.id = "app";
    app.appendChild(buildSidebar());
    app.appendChild(buildMain());
    document.body.appendChild(app);
    var pill = document.getElementById("modelpill");
    pill.querySelector(".mico").appendChild(buildModelpillIcon(active.brand));
    bindPill();
    bootIntro();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", build);
  } else {
    build();
  }
})();
