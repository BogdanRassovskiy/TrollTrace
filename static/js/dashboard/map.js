// Mapa narativů: дерево кодбука (тема → nadnarativ → narativ → podnarativ) с ползунком времени.
// Все недели всех узлов приходят одним запросом (/api/tree-map/); ползунок, режим окна,
// раскладка и подписи по зуму считаются в браузере. Раскладка дерева одна на весь период:
// меняются только размер, яркость и обводка узлов — при анимации ничего не «ездит».
(function () {
  const { config, getJSON, percent, weekLabel, escapeHtml, syncURL, setSelect } = TT;
  const PALETTE_TOPICS = ["#1565c0", "#ef6c00", "#6a1b9a", "#00838f", "#ad1457", "#558b2f",
    "#6d4c41", "#283593", "#f9a825", "#37474f", "#5e35b1", "#00695c"];
  const STANCE_COLORS = { pro: "#d32f2f", contra: "#2e7d32", neutral: "#9e9e9e" };
  const STANCE_NAMES = { pro: "pro", contra: "proti", neutral: "neutrálně" };
  // подпись узла уровня L (1 nadnarativ, 2 narativ, 3 podnarativ) — при зуме ≥ LABEL_ZOOM[L]
  const LABEL_ZOOM = { 0: 0, 1: 0, 2: 1.6, 3: 2.4 };

  const el = (id) => document.getElementById(id);
  const chart = echarts.init(el("map-chart"));
  const css = getComputedStyle(document.documentElement);
  const textColor = () => css.getPropertyValue("--text").trim() || "#222";
  const mutedColor = () => css.getPropertyValue("--muted").trim() || "#777";

  const params = new URLSearchParams(location.search);
  const state = {
    data: null,
    week: null, // index; null = poslední týden
    mode: params.get("rezim") === "okno" ? "window" : "cumulative",
    windowSize: params.get("okno") || "4",
    layout: params.get("rozvrzeni") === "sirka" ? "LR" : "radial",
    candidates: params.get("kandidati") !== "0",
    selected: params.get("uzel") || null,
    zoom: 1,
    center: null,
    timer: null,
  };
  setSelect(el("min-quality"), params.get("kvalita"));
  setSelect(el("window-size"), state.windowSize);
  el("show-candidates").checked = state.candidates;
  if (params.get("vrstvy")) {
    const chosen = params.get("vrstvy").split(",");
    el("layers").querySelectorAll("input").forEach((i) => (i.checked = chosen.includes(i.value)));
  }

  function layers() {
    return [...el("layers").querySelectorAll("input:checked")].map((i) => i.value);
  }

  // --- данные ------------------------------------------------------------------------------

  async function load() {
    stop();
    el("status").textContent = "Načítám…";
    try {
      state.data = await getJSON("/api/tree-map/", {
        min_quality: el("min-quality").value,
        layer: layers(),
      });
    } catch (err) {
      el("status").textContent = err.message;
      return;
    }
    const d = state.data;
    d.byCode = Object.fromEntries(d.nodes.map((n) => [n.code, n]));
    const topics = [...new Set(d.nodes.map((n) => n.topic))];
    d.topicColor = Object.fromEntries(topics.map((t, i) => [t, PALETTE_TOPICS[i % PALETTE_TOPICS.length]]));
    // масштаб размера — по всему периоду, чтобы при движении ползунка он не прыгал
    d.maxN = Math.max(1, ...d.nodes.map((n) => sum(d.series[n.code].n, 0, d.weeks.length - 1)));
    d.firstWeek = Object.fromEntries(
      d.nodes.map((n) => [n.code, d.series[n.code].n.findIndex((v) => v > 0)])
    );
    const slider = el("week");
    slider.max = Math.max(0, d.weeks.length - 1);
    const wanted = d.weeks.indexOf(params.get("tyden"));
    if (state.week === null || state.week > slider.max) state.week = wanted >= 0 ? wanted : Number(slider.max);
    slider.value = state.week;
    el("node-list").innerHTML = d.nodes
      .map((n) => `<option value="${escapeHtml(n.name)}"></option>`).join("");
    el("status").textContent = d.weeks.length
      ? ""
      : "Pro zvolené vrstvy a práh kvality nejsou žádné dokumenty.";
    render(true);
  }

  function sum(values, lo, hi) {
    let total = 0;
    for (let i = lo; i <= hi; i += 1) total += values[i] || 0;
    return total;
  }

  function range() {
    const hi = state.week ?? 0;
    const lo = state.mode === "window" ? Math.max(0, hi - Number(state.windowSize) + 1) : 0;
    return [lo, hi];
  }

  function stats(code) {
    const [lo, hi] = range();
    const s = state.data.series[code];
    const out = { n: sum(s.n, lo, hi) };
    for (const k of Object.keys(STANCE_NAMES)) out[k] = sum(s[k], lo, hi);
    out.total = sum(state.data.total, lo, hi);
    out.stance = out.n ? Object.keys(STANCE_NAMES).reduce((a, b) => (out[b] > out[a] ? b : a)) : null;
    return out;
  }

  // --- дерево -------------------------------------------------------------------------------

  function treeData() {
    const d = state.data;
    const visible = d.nodes.filter((n) => state.candidates || n.status !== "candidate");
    const codes = new Set(visible.map((n) => n.code));
    const kids = {};
    for (const n of visible) {
      const parent = n.parent && codes.has(n.parent) ? n.parent : `topic:${n.topic}`;
      (kids[parent] ||= []).push(n);
    }
    const build = (n, depth) => {
      const st = stats(n.code);
      const color = d.topicColor[n.topic];
      const faded = st.n === 0;
      const size = 7 + 38 * Math.sqrt(st.n / d.maxN);
      return {
        name: n.name,
        code: n.code,
        depth,
        value: st.n,
        symbol: "circle",
        symbolSize: n.code === state.selected ? size + 6 : size,
        itemStyle: {
          color,
          opacity: faded ? 0.18 : 1,
          borderColor: st.stance ? STANCE_COLORS[st.stance] : mutedColor(),
          borderWidth: st.stance ? 3 : 1,
          borderType: n.status === "candidate" ? "dashed" : "solid",
          shadowBlur: n.code === state.selected ? 12 : 0,
          shadowColor: color,
        },
        label: labelFor(n.name, n.level, faded, n.code === state.selected),
        children: (kids[n.code] || []).map((c) => build(c, depth + 1)),
      };
    };
    const topics = [...new Set(visible.map((n) => n.topic))].map((t) => {
      const sample = visible.find((n) => n.topic === t);
      return {
        name: sample.topic_name,
        code: `topic:${t}`,
        depth: 1,
        symbol: "roundRect",
        symbolSize: 14,
        itemStyle: { color: d.topicColor[t], borderWidth: 0 },
        label: {
          ...labelFor(sample.topic_name, 0, false, false, true),
          position: "top",
          rotate: 0,
          distance: 6,
          backgroundColor: css.getPropertyValue("--surface").trim() || "#fff",
          borderColor: d.topicColor[t],
          borderWidth: 1,
          borderRadius: 4,
          padding: [2, 6],
        },
        children: (kids[`topic:${t}`] || []).map((n) => build(n, 2)),
      };
    });
    // в круговой раскладке невидимая ступень отодвигает témata от středu — иначе подписи
    // тем слипаются в центре
    const spaced = state.layout === "radial"
      ? topics.map((t) => ({ name: "", code: "spacer", symbolSize: 0, label: { show: false },
        tooltip: { show: false }, itemStyle: { opacity: 0 }, children: [t] }))
      : topics;
    return {
      name: "Kódová kniha",
      code: "root",
      depth: 0,
      symbolSize: 8,
      itemStyle: { color: mutedColor() },
      label: { show: false },
      children: spaced,
    };
  }

  function labelFor(name, level, faded, selected, strong) {
    // do šířky je místa dost — popisky vždy; kruhově podle přiblížení
    const wide = state.layout !== "radial";
    const show = selected || wide || state.zoom >= (LABEL_ZOOM[level] ?? 3);
    const max = wide ? 52 : Math.round(28 + 18 * Math.max(0, state.zoom - 1));
    return {
      show,
      formatter: () => (name.length > max ? `${name.slice(0, max - 1)}…` : name),
      color: textColor(),
      opacity: faded && !selected ? 0.45 : 1,
      fontWeight: strong || selected ? "bold" : "normal",
      fontSize: strong ? 13 : 11,
    };
  }

  function render(fresh) {
    if (!state.data || !state.data.weeks.length) {
      chart.clear();
      return;
    }
    const [lo, hi] = range();
    const w = state.data.weeks;
    el("week-label").textContent = `Týden od ${weekLabel(w[hi])}`;
    el("window-label").textContent =
      state.mode === "window"
        ? `okno ${weekLabel(w[lo])} – ${weekLabel(w[hi])} · dokumentů: ${sum(state.data.total, lo, hi)}`
        : `od ${weekLabel(w[0])} · dokumentů: ${sum(state.data.total, lo, hi)}`;
    const radial = state.layout === "radial";
    const leaves = state.data.nodes.length;
    el("map-chart").style.height = radial ? "" : `${Math.max(560, leaves * 26)}px`;
    chart.resize();
    const series = {
      type: "tree",
      id: "tree",
      data: [treeData()],
      layout: radial ? "radial" : "orthogonal",
      orient: "LR",
      top: radial ? "8%" : 20,
      bottom: radial ? "8%" : 20,
      left: radial ? "8%" : 120,
      right: radial ? "8%" : 320,
      roam: true,
      expandAndCollapse: false,
      initialTreeDepth: -1,
      scaleLimit: { min: 0.4, max: 6 },
      lineStyle: { color: mutedColor(), width: 1, curveness: 0.4, opacity: 0.5 },
      label: radial ? {} : { position: "right", verticalAlign: "middle", align: "left" },
      emphasis: { focus: "relative" },
      animationDurationUpdate: 350,
    };
    if (!fresh) {
      series.zoom = state.zoom;
      if (state.center) series.center = state.center;
    } else {
      state.zoom = 1;
      state.center = null;
      chart.clear(); // иначе tree с тем же id сохраняет прежние зум и сдвиг
    }
    chart.setOption(
      {
        tooltip: { trigger: "item", confine: true, formatter: tooltip },
        series: [series],
      },
    );
    if (state.selected) showNode(state.selected);
    sync();
  }

  function tooltip(p) {
    const code = p.data.code;
    if (!code || code === "root" || code === "spacer") return "";
    if (code.startsWith("topic:")) return `<strong>${escapeHtml(p.data.name)}</strong><br>téma`;
    const n = state.data.byCode[code];
    const st = stats(code);
    const first = state.data.firstWeek[code];
    const parts = Object.keys(STANCE_NAMES)
      .filter((k) => st[k])
      .map((k) => `${STANCE_NAMES[k]} ${st[k]}`)
      .join(" · ");
    return `<div class="chart-tip"><strong>${escapeHtml(n.name)}</strong><br>
      <span class="muted">${escapeHtml(n.level_name)} · ${escapeHtml(n.topic_name)}${
        n.status === "candidate" ? " · kandidát" : ""}</span><br>
      Dokumenty v období: <strong>${st.n}</strong> (${percent(st.total ? st.n / st.total : null)} všech)<br>
      ${parts ? `${parts}<br>` : ""}
      ${first >= 0 ? `První výskyt: týden od ${weekLabel(state.data.weeks[first])}` : "Zatím bez dokumentů"}</div>`;
  }

  // --- боковая панель ------------------------------------------------------------------------

  const nodeChart = echarts.init(el("node-chart"));

  function pathOf(code) {
    const names = [];
    for (let n = state.data.byCode[code]; n; n = state.data.byCode[n.parent]) names.unshift(n.name);
    names.unshift(state.data.byCode[code].topic_name);
    return names.slice(0, -1).join(" › ");
  }

  function showNode(code) {
    const n = state.data?.byCode[code];
    if (!n) return;
    el("node-empty").hidden = true;
    el("node-detail").hidden = false;
    el("node-path").textContent = pathOf(code);
    el("node-name").textContent = n.name;
    el("node-level").textContent = n.level_name;
    el("node-status").textContent = n.status_name;
    el("node-status").className = `badge ${n.status === "candidate" ? "badge-warn" : "badge-ready"}`;
    el("node-definition").textContent = n.definition;
    el("node-link").href = window.NARRATIVE_URL.replace("KOD", encodeURIComponent(code));
    const st = stats(code);
    const first = state.data.firstWeek[code];
    el("node-stats").innerHTML = `
      <dt>Dokumenty v období</dt><dd>${st.n} (${percent(st.total ? st.n / st.total : null)} všech)</dd>
      ${Object.keys(STANCE_NAMES).map((k) => `<dt><i class="dot ring-${k}"></i> ${STANCE_NAMES[k]}</dt><dd>${st[k]}</dd>`).join("")}
      <dt>První výskyt</dt><dd>${first >= 0 ? weekLabel(state.data.weeks[first]) : "—"}</dd>`;
    const [lo, hi] = range();
    nodeChart.setOption({
      grid: { left: 30, right: 8, top: 10, bottom: 24 },
      tooltip: { trigger: "axis" },
      xAxis: {
        type: "category",
        data: state.data.weeks.map(weekLabel),
        axisLabel: { color: mutedColor(), fontSize: 10 },
      },
      yAxis: { type: "value", minInterval: 1, axisLabel: { color: mutedColor(), fontSize: 10 } },
      series: [{
        type: "bar",
        name: "Dokumenty",
        data: state.data.series[code].n,
        itemStyle: { color: state.data.topicColor[n.topic] },
        markArea: {
          silent: true,
          itemStyle: { color: "rgba(127,127,127,0.15)" },
          data: [[{ xAxis: lo }, { xAxis: hi }]],
        },
      }],
    }, true);
  }

  chart.on("click", (p) => {
    const code = p.data?.code;
    if (!code || code === "root" || code === "spacer" || code.startsWith("topic:")) return;
    state.selected = code;
    render(false);
  });

  // зум: подписи глубоких уровней появляются только при приближении
  let roamTimer = null;
  chart.on("treeroam", () => {
    clearTimeout(roamTimer);
    roamTimer = setTimeout(() => {
      const s = chart.getOption().series[0];
      const zoom = s.zoom || 1;
      state.center = s.center || null;
      if (Math.abs(zoom - state.zoom) > 0.05) {
        state.zoom = zoom;
        render(false);
      }
    }, 120);
  });

  // --- время -----------------------------------------------------------------------------

  function stop() {
    clearInterval(state.timer);
    state.timer = null;
    el("play").textContent = "▶";
    el("play").setAttribute("aria-label", "Přehrát");
  }

  function play() {
    if (state.timer) return stop();
    const max = Number(el("week").max);
    if (state.week >= max) state.week = 0;
    el("play").textContent = "❚❚";
    el("play").setAttribute("aria-label", "Pozastavit");
    state.timer = setInterval(() => {
      if (state.week >= max) return stop();
      state.week += 1;
      el("week").value = state.week;
      render(false);
    }, Number(el("speed").value));
  }

  el("play").addEventListener("click", play);
  el("speed").addEventListener("change", () => {
    if (state.timer) {
      stop();
      play();
    }
  });
  el("week").addEventListener("input", (e) => {
    state.week = Number(e.target.value);
    render(false);
  });
  document.addEventListener("keydown", (e) => {
    if (e.target.closest("input, select, textarea") || !state.data) return;
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    const max = Number(el("week").max);
    state.week = Math.min(max, Math.max(0, state.week + (e.key === "ArrowRight" ? 1 : -1)));
    el("week").value = state.week;
    render(false);
  });
  document.querySelectorAll(".mode-btn").forEach((b) => b.addEventListener("click", () => {
    state.mode = b.dataset.mode;
    render(false);
  }));
  el("window-size").addEventListener("change", (e) => {
    state.windowSize = e.target.value;
    render(false);
  });

  // --- фильтры ---------------------------------------------------------------------------

  document.querySelectorAll(".layout-btn").forEach((b) => b.addEventListener("click", () => {
    state.layout = b.dataset.layout;
    render(true);
  }));
  el("show-candidates").addEventListener("change", (e) => {
    state.candidates = e.target.checked;
    render(true);
  });
  el("min-quality").addEventListener("change", load);
  el("layers").addEventListener("change", load);
  el("node-search").addEventListener("change", (e) => {
    const n = state.data?.nodes.find((x) => x.name === e.target.value || x.code === e.target.value);
    if (!n) return;
    if (n.status === "candidate" && !state.candidates) {
      state.candidates = true;
      el("show-candidates").checked = true;
    }
    state.selected = n.code;
    if (state.layout === "radial") state.zoom = Math.max(state.zoom, 1.7);
    render(false);
    e.target.value = "";
  });

  function sync() {
    document.querySelectorAll(".mode-btn").forEach((b) => b.classList.toggle("active", b.dataset.mode === state.mode));
    document.querySelectorAll(".layout-btn").forEach((b) => b.classList.toggle("active", b.dataset.layout === state.layout));
    el("window-size").disabled = state.mode !== "window";
    const all = [...el("layers").querySelectorAll("input")];
    syncURL({
      tyden: state.data?.weeks[state.week],
      rezim: state.mode === "window" ? "okno" : "",
      okno: state.mode === "window" ? state.windowSize : "",
      rozvrzeni: state.layout === "LR" ? "sirka" : "",
      kandidati: state.candidates ? "" : "0",
      kvalita: el("min-quality").value !== String(config.min_quality) ? el("min-quality").value : "",
      vrstvy: layers().length === all.length ? "" : layers().join(","),
      uzel: state.selected || "",
    });
  }

  window.addEventListener("resize", () => {
    chart.resize();
    nodeChart.resize();
  });
  load();
})();
