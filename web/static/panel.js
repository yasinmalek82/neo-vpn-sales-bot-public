/* Panel glue. Telegram sends initData to the page, not to the server, so the
   page puts it in a header on every request. No data is written to the page
   from here except fixed Persian lines. */
(function () {
  "use strict";
  var tg = window.Telegram && window.Telegram.WebApp;
  var initData = tg && tg.initData ? tg.initData : "";

  if (tg) {
    try {
      tg.ready();
      tg.expand();
      tg.setHeaderColor("#0B0F14");
      tg.setBackgroundColor("#0B0F14");
    } catch (e) { /* an old client lacks some of these: the page still works */ }
  }

  document.addEventListener("htmx:configRequest", function (event) {
    event.detail.headers["X-Telegram-Init-Data"] = initData;
  });

  /* A refusal (4xx) is a short Persian line the user should read: a wrong
     price (400), a missing group (404), a stale form (409). */
  document.addEventListener("htmx:beforeSwap", function (event) {
    var status = event.detail.xhr.status;
    if (status >= 400 && status < 500) {
      event.detail.shouldSwap = true;
      event.detail.isError = false;
    }
  });

  function showFailure() {
    var main = document.getElementById("main");
    if (!main) { return; }
    var p = document.createElement("p");
    p.className = "empty";
    p.textContent = "نشد بخوانم. دوباره امتحان کن.";
    main.replaceChildren(p);
  }
  document.addEventListener("htmx:responseError", showFailure);
  document.addEventListener("htmx:sendError", showFailure);

  /* The current section, in both navigations; the phone's bar scrolls it into view. */
  function markSection(url) {
    var links = document.querySelectorAll("[data-nav]");
    for (var i = 0; i < links.length; i++) {
      if (links[i].getAttribute("hx-get") === url) {
        links[i].setAttribute("aria-current", "page");
        if (links[i].parentElement && links[i].parentElement.id === "tabbar") {
          links[i].scrollIntoView({ block: "nearest", inline: "nearest" });
        }
      } else {
        links[i].removeAttribute("aria-current");
      }
    }
  }

  document.addEventListener("click", function (event) {
    var link = event.target instanceof Element ? event.target.closest("[data-nav]") : null;
    if (!link) { return; }
    event.preventDefault();
    markSection(link.getAttribute("hx-get"));
  });

  /* A screen reached by a link inside a page (a reseller's sheet from a customer's card)
     belongs to a section too: the longest navigation address its path starts with. A new
     screen starts at its top; a tab or a filter of the same screen (same path, another
     query) and every form stay where the reader is. */
  var lastPath = "";
  document.addEventListener("htmx:afterSettle", function (event) {
    var detail = event.detail;
    if (!detail.target || detail.target.id !== "main" || !detail.pathInfo) { return; }
    var path = String(detail.pathInfo.requestPath || "").split("?")[0];
    var links = document.querySelectorAll("[data-nav]");
    var best = "";
    for (var i = 0; i < links.length; i++) {
      var url = links[i].getAttribute("hx-get") || "";
      if ((path === url || path.indexOf(url + "/") === 0) && url.length > best.length) { best = url; }
    }
    if (best) { markSection(best); }
    var verb = detail.requestConfig && detail.requestConfig.verb;
    if (verb === "get" && lastPath && path !== lastPath) { window.scrollTo(0, 0); }
    if (verb === "get") { lastPath = path; }
  });

  /* An emoji in a line the bot wrote is drawn here as a line icon, in the navigation's
     own stroke. The server's text is untouched (the bot's words, byte for byte, which the
     tests compare); this only draws them. An emoji with no icon below stays an emoji. The
     preview of a Telegram button keeps its emoji: that is what the customer will see. An
     <option> cannot hold an icon, so its emoji is dropped. Each entry: path, tone. */
  var CIRCLE = "M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20z";
  var CLOCK = CIRCLE + "M12 6v6l4 2";
  var WALLET = "M4 7h15a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1zM4 7l12-3v3M16 13h2";
  var USER = "M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2M12 3a4 4 0 1 0 0 8 4 4 0 0 0 0-8z";
  var REFRESH = "M23 4v6h-6M1 20v-6h6M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15";
  var ICONS = {
    "✅": ["M20 6 9 17l-5-5", "ok"],
    "✔": ["M20 6 9 17l-5-5", "ok"],
    "❌": ["M18 6 6 18M6 6l12 12", "bad"],
    "⚠": ["M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0zM12 9v4M12 17h.01", "warn"],
    "ℹ": [CIRCLE + "M12 16v-4M12 8h.01", ""],
    "\u{1F6AB}": [CIRCLE + "M4.93 4.93l14.14 14.14", "bad"],
    "⛔": [CIRCLE + "M4.93 4.93l14.14 14.14", "bad"],
    "➕": ["M12 5v14M5 12h14", ""],
    "↩": ["M9 14 4 9l5-5M20 20v-7a4 4 0 0 0-4-4H4", ""],
    "↕": ["M12 3v18M8 7l4-4 4 4M8 17l4 4 4-4", ""],
    "↔": ["M18 8l4 4-4 4M2 12h20M6 8l-4 4 4 4", ""],
    "\u{1F501}": [REFRESH, ""],
    "\u{1F504}": [REFRESH, ""],
    "✋": ["M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01", ""],
    "\u{1F590}": ["M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01", ""],
    "✏": ["M17 3a2.83 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z", ""],
    "\u{1F5D1}": ["M3 6h18M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M10 11v6M14 11v6", ""],
    "\u{1F6D2}": ["M9 20a1 1 0 1 0 0 2 1 1 0 0 0 0-2zM20 20a1 1 0 1 0 0 2 1 1 0 0 0 0-2zM1 1h4l2.68 13.39a2 2 0 0 0 2 1.61h9.72a2 2 0 0 0 2-1.61L23 6H6", ""],
    "⏱": [CLOCK, ""],
    "⏳": [CLOCK, ""],
    "\u{1F465}": ["M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2M9 3a4 4 0 1 0 0 8 4 4 0 0 0 0-8zM23 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75", ""],
    "\u{1F464}": [USER, ""],
    "\u{1F44B}": [USER, ""],
    "\u{1F39F}": ["M2 9a3 3 0 0 1 0 6v2a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-2a3 3 0 0 1 0-6V7a2 2 0 0 0-2-2H4a2 2 0 0 0-2 2zM13 5v2M13 17v2M13 11v2", ""],
    "\u{1F5A5}": ["M4 3h16a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2zM8 21h8M12 17v4", ""],
    "\u{1F517}": ["M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71", ""],
    "\u{1F522}": ["M4 9h16M4 15h16M10 3 8 21M16 3l-2 18", ""],
    "\u{1F3EA}": ["M4 9l1.5-5h13L20 9M4 9v10h16V9M4 9h16M9 19v-5h6v5", ""],
    "\u{1F3E6}": [WALLET, ""],
    "\u{1F4B0}": [WALLET, ""],
    "\u{1F4B3}": [WALLET, ""],
    "\u{1F9E9}": ["M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6", ""],
    "\u{1F9EA}": ["M10 2v7.53a2 2 0 0 1-.21.9L4.72 20.55a1 1 0 0 0 .9 1.45h12.76a1 1 0 0 0 .9-1.45l-5.07-10.12a2 2 0 0 1-.21-.9V2M8.5 2h7M7 16h10", ""],
    "\u{1F381}": ["M20 12v10H4V12M2 7h20v5H2zM12 22V7M12 7H7.5a2.5 2.5 0 0 1 0-5C11 2 12 7 12 7zM12 7h4.5a2.5 2.5 0 0 0 0-5C13 2 12 7 12 7z", ""],
    "\u{1F3F7}": ["M3 12V4h8l10 10-8 8L3 12zM8.7 8.5a1.2 1.2 0 1 1-2.4 0 1.2 1.2 0 0 1 2.4 0z", ""],
    "\u{1F4E3}": ["M4 14v-4l11-5v14L4 14zM7 14l1 6h3l-1-5", ""],
    "\u{1F50C}": ["M13 2 3 14h9l-1 8 10-12h-9l1-8z", ""],
    "\u{1F4E1}": ["M12 10a2 2 0 1 0 0 4 2 2 0 0 0 0-4zM16.24 7.76a6 6 0 0 1 0 8.49M7.76 16.24a6 6 0 0 1 0-8.49M19.07 4.93a10 10 0 0 1 0 14.14M4.93 19.07a10 10 0 0 1 0-14.14", ""],
    "\u{1F3A8}": ["M5 3h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2zM8.5 7a1.5 1.5 0 1 0 0 3 1.5 1.5 0 0 0 0-3zM21 15l-5-5L5 21", ""],
    "\u{1F5D3}": ["M5 4h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2zM16 2v4M8 2v4M3 10h18", ""],
    "\u{1F4C5}": ["M5 4h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2zM16 2v4M8 2v4M3 10h18", ""],
    "\u{1F441}": ["M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8zM12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6z", ""],
    "\u{1F514}": ["M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9M13.73 21a2 2 0 0 1-3.46 0", ""],
    "\u{1F512}": ["M5 11h14a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2zM7 11V7a5 5 0 0 1 10 0v4", ""],
    "\u{1F4CA}": ["M4 20V10M10 20V4M16 20v-7M21 20H3", ""],
    "\u{1F4E6}": ["M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16zM3.27 6.96 12 12.01l8.73-5.05M12 22.08V12", ""],
    "♾": ["M18.18 8c5.1 0 5.1 8 0 8-5.1 0-7.13-8-12.74-8-4.58 0-4.58 8 0 8 5.6 0 7.64-8 12.74-8z", ""]
  };
  var SVG_NS = "http://www.w3.org/2000/svg";
  var EMOJI_SPLIT = null;
  try { EMOJI_SPLIT = new RegExp("(\\p{Extended_Pictographic}\\uFE0F?)", "u"); } catch (e) { /* an old engine keeps the emoji */ }

  function iconNode(spec) {
    var svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("class", "em" + (spec[1] ? " " + spec[1] : ""));
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    var path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", spec[0]);
    svg.appendChild(path);
    return svg;
  }

  function drawIcons(root) {
    if (!EMOJI_SPLIT || !root || !document.createTreeWalker) { return; }
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    var found = [];
    var node;
    while ((node = walker.nextNode())) {
      if (EMOJI_SPLIT.test(node.nodeValue)) { found.push(node); }
    }
    for (var i = 0; i < found.length; i++) {
      var text = found[i];
      var parent = text.parentElement;
      if (!parent || parent.closest(".tg-btn, #plan-preview, textarea, script, style")) { continue; }
      var parts = text.nodeValue.split(EMOJI_SPLIT);
      if (parent.closest("option")) {
        var plain = "";
        for (var k = 0; k < parts.length; k++) { if (k % 2 === 0) { plain += parts[k]; } }
        text.nodeValue = plain.replace(/\s+/g, " ").replace(/^ | $/g, "");
        continue;
      }
      var frag = document.createDocumentFragment();
      for (var j = 0; j < parts.length; j++) {
        if (!parts[j]) { continue; }
        var spec = j % 2 === 1 ? ICONS[parts[j].replace("️", "")] : null;
        frag.appendChild(spec ? iconNode(spec) : document.createTextNode(parts[j]));
      }
      parent.replaceChild(frag, text);
    }
  }
  document.addEventListener("htmx:afterSwap", function (event) { drawIcons(event.detail.target); });

  /* Requests in flight: the stylesheet draws a line on top while there is one. */
  var inFlight = 0;
  function busy(delta) {
    inFlight = Math.max(0, inFlight + delta);
    document.documentElement.classList.toggle("busy", inFlight > 0);
  }
  document.addEventListener("htmx:beforeRequest", function () { busy(1); });
  document.addEventListener("htmx:afterRequest", function () { busy(-1); });
}());
