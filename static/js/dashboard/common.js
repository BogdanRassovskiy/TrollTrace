// Общие функции рабочей части (этап 8): данные из /api/…, цвета, панель с постами.
window.TT = (function () {
  const config = JSON.parse(document.getElementById("chart-config")?.textContent || "{}");

  // Цвет по слою: линии красятся по слою или партии, а не уникально на актора (README §7.1).
  const LAYER_COLORS = {
    A: "#c62828", B: "#ef6c00", C: "#8e24aa", D: "#1565c0", E: "#2e7d32",
    F: "#00838f", G: "#6d4c41", H: "#455a64", I: "#7b1fa2",
  };
  const PALETTE = ["#1565c0", "#c62828", "#2e7d32", "#ef6c00", "#6a1b9a", "#00838f", "#ad1457",
    "#558b2f", "#4e342e", "#283593", "#f9a825", "#37474f"];
  const STANCES = { pro: "pro", contra: "proti", neutral: "neutrálně" };

  function partyColor(party) {
    if (!party) return "#9e9e9e";
    let hash = 0;
    for (const ch of party) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
    return PALETTE[hash % PALETTE.length];
  }

  function layerColor(layer) {
    return LAYER_COLORS[layer] || "#9e9e9e";
  }

  async function getJSON(url, params) {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(params || {})) {
      if (Array.isArray(value)) value.forEach((v) => query.append(key, v));
      else if (value !== undefined && value !== null && value !== "") query.append(key, value);
    }
    const response = await fetch(`${url}?${query}`, { credentials: "same-origin" });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `Chyba ${response.status}`);
    return data;
  }

  function percent(value, digits = 1) {
    return value === null || value === undefined ? "—" : `${(value * 100).toFixed(digits)} %`;
  }

  function weekLabel(iso) {
    const [y, m, d] = iso.split("-");
    return `${Number(d)}. ${Number(m)}. ${y}`;
  }

  function escapeHtml(text) {
    return String(text ?? "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[c]);
  }

  // Панель «příspěvky za bodem grafu».
  const panel = document.getElementById("posts");
  document.getElementById("posts-close")?.addEventListener("click", () => { panel.hidden = true; });

  async function showPosts(params, title) {
    if (!panel) return;
    panel.hidden = false;
    document.getElementById("posts-title").textContent = title;
    const meta = document.getElementById("posts-meta");
    const list = document.getElementById("posts-list");
    meta.textContent = "Načítám…";
    list.innerHTML = "";
    try {
      const data = await getJSON("/api/documents/", params);
      meta.textContent = data.count
        ? `Dokumentů s narativem: ${data.count}${data.count > data.documents.length ? ` (zobrazeno ${data.documents.length})` : ""}.`
        : "V tomto bodě nejsou žádné dokumenty s narativem.";
      list.innerHTML = data.documents.map((d) => `
        <li>
          <a href="${escapeHtml(d.url)}" target="_blank" rel="noopener">${escapeHtml(d.title || d.url)}</a>
          <div class="muted small">${escapeHtml(d.actor)} · ${escapeHtml(d.source)} (${escapeHtml(d.layer)})
            · ${d.published_at ? new Date(d.published_at).toLocaleDateString("cs-CZ") : "?"}
            · <span class="stance-${d.stance}">${STANCES[d.stance] || d.stance}</span>
            ${d.origin === "human" ? "· ruční štítek" : ""}
            ${d.carrier && d.carrier !== "autor" ? `· nese: ${escapeHtml(d.carrier)}` : ""}</div>
          ${d.evidence ? `<p class="evidence small">„${escapeHtml(d.evidence)}“</p>` : ""}
        </li>`).join("");
    } catch (error) {
      meta.textContent = error.message;
    }
    panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  // Ссылка с настройками графика: состояние живёт в GET-параметрах адреса.
  function syncURL(params) {
    const url = new URL(window.location);
    url.search = "";
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== null && value !== "") url.searchParams.set(key, value);
    }
    window.history.replaceState(null, "", url);
    return url.toString();
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text); // только HTTPS или localhost
      return true;
    } catch {
      const area = Object.assign(document.createElement("textarea"), { value: text });
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.select();
      const ok = document.execCommand("copy");
      area.remove();
      return ok;
    }
  }

  function bindShareButton() {
    const button = document.getElementById("share-link");
    const note = document.getElementById("share-note");
    if (!button) return;
    button.addEventListener("click", async () => {
      const url = window.location.href;
      if (await copyText(url)) {
        note.textContent = "Odkaz zkopírován do schránky.";
      } else {
        window.prompt("Zkopírujte odkaz:", url);
        note.textContent = "";
      }
      clearTimeout(bindShareButton.timer);
      bindShareButton.timer = setTimeout(() => { note.textContent = ""; }, 3000);
    });
  }
  bindShareButton();

  // Выбор значения в <select>, только если такая опция есть (ссылку могли поправить руками).
  function setSelect(select, value) {
    if (value !== null && [...select.options].some((o) => o.value === value)) select.value = value;
  }

  function onResize(chart) {
    window.addEventListener("resize", () => chart.resize());
  }

  // --- Дерево кодбука: тема → nadnarativ → narativ → podnarativ (этап 6.2) ---------------
  // nodes: [{code, name, parent, level, topic, topic_name}] → [{topic, name, children: [узлы]}],
  // у узла children; родитель вне набора (не активен) — узел становится корнем темы.
  function buildTree(nodes) {
    const codes = new Set(nodes.map((n) => n.code));
    const items = Object.fromEntries(nodes.map((n) => [n.code, { ...n, children: [] }]));
    const topics = new Map();
    for (const n of nodes) {
      const item = items[n.code];
      if (n.parent && codes.has(n.parent)) items[n.parent].children.push(item);
      else {
        if (!topics.has(n.topic)) topics.set(n.topic, { topic: n.topic, name: n.topic_name, children: [] });
        topics.get(n.topic).children.push(item);
      }
    }
    const order = (list) => {
      list.sort((a, b) => (a.level || 0) - (b.level || 0) || a.name.localeCompare(b.name, "cs"));
      list.forEach((x) => order(x.children));
      return list;
    };
    return order([...topics.values()]);
  }

  const LEVELS = { 1: "Nadnarativ", 2: "Narativ", 3: "Podnarativ" };

  // Вложенный раскрывающийся список. row(item) → HTML строки узла; ветки — <details>.
  // open: Set кодов раскрытых веток (и "topic:…" для тем); undefined — всё раскрыто.
  // topicRow(t) → HTML заголовка темы (например, с флажком); без него — только название.
  function treeHTML(tree, row, open, topicRow) {
    const isOpen = (key) => !open || open.has(key);
    const node = (item) => {
      const search = escapeHtml(`${item.name} ${item.code}`.toLocaleLowerCase("cs"));
      if (!item.children.length) {
        return `<li class="tt-node" data-search="${search}"><div class="tt-leaf">${row(item)}</div></li>`;
      }
      return `<li class="tt-node" data-search="${search}"><details data-key="${escapeHtml(item.code)}" ${isOpen(item.code) ? "open" : ""}>
        <summary>${row(item)}</summary><ul class="tt-list">${item.children.map(node).join("")}</ul></details></li>`;
    };
    return tree.map((t) => `<li class="tt-topic"><details data-key="topic:${escapeHtml(t.topic)}" ${isOpen(`topic:${t.topic}`) ? "open" : ""}>
      <summary class="tt-topic-name">${topicRow ? topicRow(t) : escapeHtml(t.name)}</summary>
      <ul class="tt-list">${t.children.map(node).join("")}</ul></details></li>`).join("");
  }

  // Поиск в дереве: совпавшие узлы и путь к ним видимы, ветки с совпадениями раскрыты.
  function filterTree(root, query) {
    const q = query.trim().toLocaleLowerCase("cs");
    const nodes = root.querySelectorAll(".tt-node");
    const topics = root.querySelectorAll(".tt-topic");
    if (!q) {
      nodes.forEach((li) => li.classList.remove("tt-hidden", "tt-match"));
      topics.forEach((li) => li.classList.remove("tt-hidden"));
      return;
    }
    nodes.forEach((li) => li.classList.add("tt-hidden"));
    nodes.forEach((li) => {
      li.classList.toggle("tt-match", li.dataset.search.includes(q));
      if (!li.classList.contains("tt-match")) return;
      li.classList.remove("tt-hidden");
      li.querySelectorAll(".tt-node").forEach((c) => c.classList.remove("tt-hidden"));
      for (let p = li.parentElement; p && p !== root; p = p.parentElement) {
        if (p.classList.contains("tt-node")) p.classList.remove("tt-hidden");
        if (p.tagName === "DETAILS") p.open = true;
      }
    });
    topics.forEach((t) => t.classList.toggle("tt-hidden", !t.querySelector(".tt-node:not(.tt-hidden)")));
  }

  function openKeys(root) {
    return new Set([...root.querySelectorAll("details[data-key]")].filter((d) => d.open).map((d) => d.dataset.key));
  }

  // Выбор одного узла дерева вместо длинного <select>: кнопка → всплывающее дерево с поиском.
  // Значение — в скрытом поле input (на нём событие change, как у select).
  function treePicker({ prefix, input, nodes }) {
    const $ = (id) => document.getElementById(`${prefix}-${id}`);
    const button = $("button"), pop = $("pop"), search = $("search"), list = $("tree");
    if (!button) return;
    const byCode = Object.fromEntries(nodes.map((n) => [n.code, n]));
    const tree = buildTree(nodes);
    const label = () => {
      const n = byCode[input.value];
      $("current").textContent = n ? n.name : "— vyberte narativ —";
      $("level").textContent = n ? LEVELS[n.level] || "" : "";
    };
    const path = (code) => {
      const keys = new Set();
      for (let n = byCode[code]; n; n = byCode[n.parent]) {
        keys.add(`topic:${n.topic}`);
        if (n.parent) keys.add(n.parent);
      }
      return keys;
    };
    const row = (n) => `<button type="button" class="tt-pick${n.code === input.value ? " selected" : ""}" data-code="${escapeHtml(n.code)}">
        <span class="badge tree-level-${n.level}">${LEVELS[n.level] || ""}</span> ${escapeHtml(n.name)}
        ${n.children.length ? `<span class="muted small">· větev: ${n.children.length}</span>` : ""}</button>`;
    function open() {
      list.innerHTML = treeHTML(tree, row, path(input.value));
      search.value = "";
      pop.hidden = false;
      button.setAttribute("aria-expanded", "true");
      search.focus();
      list.querySelector(".selected")?.scrollIntoView({ block: "center" });
    }
    function close() {
      pop.hidden = true;
      button.setAttribute("aria-expanded", "false");
    }
    button.addEventListener("click", () => (pop.hidden ? open() : close()));
    search.addEventListener("input", () => filterTree(list, search.value));
    search.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        list.querySelector(".tt-match .tt-pick, .tt-match > details > summary .tt-pick")?.click();
      }
    });
    list.addEventListener("click", (e) => {
      const pick = e.target.closest(".tt-pick");
      if (!pick) return;
      e.preventDefault(); // klik na název ve <summary> vybírá, nerozbaluje
      input.value = pick.dataset.code;
      label();
      close();
      input.dispatchEvent(new Event("change"));
    });
    document.addEventListener("click", (e) => {
      if (!pop.hidden && !e.target.closest(`#${prefix}-picker`)) close();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !pop.hidden) {
        close();
        button.focus();
      }
    });
    label();
    return { refresh: label };
  }

  return { config, layerColor, partyColor, getJSON, percent, weekLabel, escapeHtml, showPosts,
    onResize, syncURL, setSelect, STANCES, buildTree, treeHTML, filterTree, openKeys, LEVELS,
    treePicker };
})();
