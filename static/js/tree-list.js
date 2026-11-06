// Раскрывающиеся списки дерева кодбука: поиск, «развернуть / свернуть всё»,
// запоминание раскрытых веток в браузере (только удобство, без него всё работает).
(function () {
  const root = document.querySelector("[data-tree]");
  if (!root) return;
  const KEY = "trolltrace-tree-open:" + location.pathname;
  const all = () => root.querySelectorAll("details[data-key]");

  function load() {
    try {
      return JSON.parse(localStorage.getItem(KEY) || "null");
    } catch (e) {
      return null;
    }
  }
  function save() {
    const open = [...all()].filter((d) => d.open).map((d) => d.dataset.key);
    try {
      localStorage.setItem(KEY, JSON.stringify(open));
    } catch (e) {
      /* úložiště nedostupné */
    }
  }

  const saved = load();
  if (saved) all().forEach((d) => (d.open = saved.includes(d.dataset.key)));
  root.addEventListener("toggle", save, true);

  function setAll(open) {
    all().forEach((d) => (d.open = open));
    save();
  }
  document.querySelector("[data-tree-open]")?.addEventListener("click", () => setAll(true));
  document.querySelector("[data-tree-close]")?.addEventListener("click", () => {
    root.querySelectorAll(".tree-node details").forEach((d) => (d.open = false));
    save();
  });

  // поиск: показываем совпавшие узлы и путь к ним, ветки с совпадениями раскрываем
  const input = document.querySelector("[data-tree-filter]");
  const counter = document.querySelector("[data-tree-count]");
  const strip = (s) => s.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
  input?.addEventListener("input", () => {
    const q = strip(input.value.trim());
    const nodes = root.querySelectorAll(".tree-node");
    if (!q) {
      nodes.forEach((li) => li.classList.remove("tree-hidden", "tree-match"));
      root.querySelectorAll(".tree-topic").forEach((t) => t.classList.remove("tree-hidden"));
      counter.textContent = "";
      return;
    }
    let hits = 0;
    nodes.forEach((li) => li.classList.add("tree-hidden"));
    nodes.forEach((li) => {
      li.classList.remove("tree-match");
      if (!strip(li.dataset.search).includes(q)) return;
      hits += 1;
      li.classList.add("tree-match");
      li.classList.remove("tree-hidden");
      li.querySelectorAll(".tree-node").forEach((c) => c.classList.remove("tree-hidden"));
      for (let p = li.parentElement; p && p !== root; p = p.parentElement) {
        if (p.classList.contains("tree-node")) p.classList.remove("tree-hidden");
        if (p.tagName === "DETAILS") p.open = true;
      }
    });
    root.querySelectorAll(".tree-topic").forEach((t) => {
      t.classList.toggle("tree-hidden", !t.querySelector(".tree-node:not(.tree-hidden)"));
    });
    counter.textContent = hits ? `nalezeno: ${hits}` : "nic nenalezeno";
  });

  // Enter v hledání nesmí odeslat formulář návrhu
  input?.addEventListener("keydown", (e) => e.key === "Enter" && e.preventDefault());

  // черновик дерева: смена родителя сразу переносит узел в предпросмотре
  function recount() {
    root.querySelectorAll("[data-count]").forEach((el) => {
      const list = root.querySelector(`ul[data-children="${CSS.escape(el.dataset.count)}"]`);
      el.textContent = list ? list.querySelectorAll(":scope > li").length : 0;
    });
  }
  root.addEventListener("change", (e) => {
    const t = e.target;
    if (t.matches("[data-accept]")) {
      t.closest(".draft-group").classList.toggle("draft-rejected", !t.checked);
    }
    if (!t.matches("[data-move]")) return;
    const li = t.closest("li");
    const target = root.querySelector(`ul[data-children="${CSS.escape(t.value)}"]`);
    if (!target || li.contains(target)) return; // pod sebe samého — ukáže kontrola při uložení
    target.append(li);
    for (let p = target.parentElement; p && p !== root; p = p.parentElement) {
      if (p.tagName === "DETAILS") p.open = true;
    }
    li.classList.add("draft-moved");
    recount();
    li.scrollIntoView({ block: "nearest", behavior: "smooth" });
  });
})();
