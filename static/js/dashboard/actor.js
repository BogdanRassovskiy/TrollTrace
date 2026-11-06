// Страница актора: доли нарративов по неделям, отметки всплесков, клик → посты (README §7.1).
(function () {
  const { config, getJSON, percent, weekLabel, escapeHtml, showPosts, onResize } = window.TT;
  const el = document.getElementById("actor-chart");
  if (!el || !window.echarts) return;
  const chart = echarts.init(el, null, { renderer: "canvas" });
  onResize(chart);
  const controls = {
    stance: document.getElementById("stance"),
    quality: document.getElementById("min-quality"),
    smoothed: document.getElementById("smoothed"),
  };
  let data = null;
  const $ = (id) => document.getElementById(id);
  const PALETTE = ["#1565c0", "#ef6c00", "#2e7d32", "#c62828", "#6a1b9a", "#00838f", "#ad1457",
    "#558b2f", "#4e342e", "#f9a825"];
  const TREE = config.tree || [];
  const ACTIVE = new Set(TREE.map((n) => n.code));
  // výchozí: jen nejvyšší úroveň stromu, větev se nezobrazuje dvakrát
  const nested = () => TREE.filter((n) => n.parent && ACTIVE.has(n.parent)).map((n) => n.code);
  let off = new Set();
  const colorOf = (code) => {
    const i = data ? data.series.findIndex((s) => s.code === code) : -1;
    return i < 0 ? "#9e9e9e" : PALETTE[i % PALETTE.length];
  };

  async function load() {
    chart.showLoading({ text: "Načítám…" });
    data = await getJSON("/api/actor/", {
      actor: config.actor, stance: controls.stance.value, min_quality: controls.quality.value,
    }).finally(() => chart.hideLoading());
    controls.smoothed.disabled = controls.stance.value !== "all";
    draw();
  }

  function applyURL() {
    const p = new URLSearchParams(window.location.search);
    window.TT.setSelect(controls.stance, p.get("stance"));
    window.TT.setSelect(controls.quality, p.get("q"));
    controls.smoothed.checked = p.get("smoothed") === "1";
    off = p.has("nx") ? new Set(p.get("nx").split(",").filter((c) => ACTIVE.has(c))) : new Set(nested());
  }

  function renderTree() {
    const list = $("actor-tree");
    if (!data) return;
    const present = new Set(data.series.map((s) => s.code));
    const totals = Object.fromEntries(data.series.map((s) => [s.code, s.n.reduce((a, b) => a + (b || 0), 0)]));
    const open = list.children.length ? TT.openKeys(list) : undefined;
    const tree = TT.buildTree(TREE.filter((n) => present.has(n.code)));
    list.innerHTML = TT.treeHTML(tree, (n) => {
      const on = !off.has(n.code);
      return `<label class="tt-check${on ? "" : " off"}" data-pick="${escapeHtml(n.code)}"><input type="checkbox" ${on ? "checked" : ""}>
        <span class="swatch" style="background:${colorOf(n.code)}"></span>
        <span class="name" title="${escapeHtml(n.name)}">${escapeHtml(n.name)}</span>
        <span class="muted small">${totals[n.code] || 0}</span></label>`;
    }, open);
    TT.filterTree(list, $("tree-search").value);
  }

  function syncURL() {
    window.TT.syncURL({
      stance: controls.stance.value === "all" ? "" : controls.stance.value,
      q: controls.quality.value === String(config.min_quality) ? "" : controls.quality.value,
      smoothed: controls.smoothed.checked ? "1" : "",
      nx: [...off].join(",") || "-",
    });
  }

  function draw() {
    syncURL();
    renderTree();
    const empty = !data.series.length;
    document.getElementById("chart-empty").hidden = !empty;
    el.hidden = empty;
    if (empty) return;
    const useSmoothed = controls.smoothed.checked && !controls.smoothed.disabled;
    const series = [];
    data.series.forEach((s, k) => {
      if (off.has(s.code)) return;
      const values = useSmoothed ? s.smoothed : s.share;
      const color = PALETTE[k % PALETTE.length];
      series.push({
        name: s.name, id: s.code, type: "line", data: values, connectNulls: false, color,
        symbolSize: 6, emphasis: { focus: "series" },
      });
      const spikes = s.z.map((z, i) => (z !== null && z >= data.spike_z && s.n[i] > 0
        ? [i, values[i]] : null)).filter(Boolean);
      if (spikes.length) {
        series.push({
          name: `${s.name} — výkyv`, id: `${s.code}::spike`, type: "scatter", data: spikes,
          symbol: "diamond", symbolSize: 14, z: 5, tooltip: { show: false },
          itemStyle: { color, borderColor: "#000", borderWidth: 1 },
        });
      }
    });
    chart.setOption({
      animation: false,
      grid: { left: 56, right: 24, top: 16, bottom: 40 },
      tooltip: {
        trigger: "axis",
        formatter(params) {
          const i = params[0].dataIndex;
          const rows = data.series.map((s) => {
            if (s.total[i] === null || off.has(s.code)) return "";
            const z = s.z[i] !== null ? ` · z ${s.z[i].toFixed(1)}` : "";
            const mark = s.z[i] !== null && s.z[i] >= data.spike_z && s.n[i] > 0 ? " ◆" : "";
            return `<div>${escapeHtml(s.name)}: <b>${percent(s.share[i])}</b>
              (${s.n[i]} z ${s.total[i]})${z}${mark}</div>`;
          }).join("");
          return `<b>Týden od ${weekLabel(data.weeks[i])}</b>${rows || "<div>bez dokumentů</div>"}`;
        },
      },
      xAxis: { type: "category", data: data.weeks.map(weekLabel) },
      yAxis: { type: "value", axisLabel: { formatter: (v) => percent(v, 0) }, min: 0 },
      series,
    }, true);
  }

  chart.on("click", (params) => {
    if (!data || params.seriesType === "scatter" && !params.seriesId) return;
    const code = String(params.seriesId).split("::")[0];
    const index = Array.isArray(params.value) ? params.value[0] : params.dataIndex;
    const s = data.series.find((x) => x.code === code);
    if (!s || s.total[index] === null) return;
    showPosts(
      { narrative: code, actor: config.actor, week: data.weeks[index],
        min_quality: controls.quality.value, stance: controls.stance.value },
      `${s.name} · týden od ${weekLabel(data.weeks[index])}`,
    );
  });

  $("actor-tree").addEventListener("change", (e) => {
    const code = e.target.closest("[data-pick]")?.dataset.pick;
    if (!code) return;
    if (e.target.checked) off.delete(code); else off.add(code);
    draw();
  });
  $("tree-search").addEventListener("input", () => TT.filterTree($("actor-tree"), $("tree-search").value));
  $("tree-top").addEventListener("click", () => { off = new Set(nested()); draw(); });
  $("tree-all").addEventListener("click", () => { off = new Set(); draw(); });
  $("tree-none").addEventListener("click", () => { off = new Set(TREE.map((n) => n.code)); draw(); });
  controls.stance.addEventListener("change", load);
  controls.quality.addEventListener("change", load);
  controls.smoothed.addEventListener("change", draw);
  applyURL();
  load().catch((error) => { el.replaceWith(Object.assign(document.createElement("p"), {
    className: "msg msg-error", textContent: error.message })); });
})();
