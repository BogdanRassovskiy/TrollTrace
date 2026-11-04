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
  }

  function syncURL() {
    window.TT.syncURL({
      stance: controls.stance.value === "all" ? "" : controls.stance.value,
      q: controls.quality.value === String(config.min_quality) ? "" : controls.quality.value,
      smoothed: controls.smoothed.checked ? "1" : "",
    });
  }

  function draw() {
    syncURL();
    const empty = !data.series.length;
    document.getElementById("chart-empty").hidden = !empty;
    el.hidden = empty;
    if (empty) return;
    const useSmoothed = controls.smoothed.checked && !controls.smoothed.disabled;
    const series = [];
    const palette = ["#1565c0", "#ef6c00", "#2e7d32", "#c62828", "#6a1b9a", "#00838f", "#ad1457",
      "#558b2f", "#4e342e", "#f9a825"];
    data.series.forEach((s, k) => {
      const values = useSmoothed ? s.smoothed : s.share;
      const color = palette[k % palette.length];
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
      legend: { type: "scroll", top: 0, data: data.series.map((s) => s.name) },
      grid: { left: 56, right: 24, top: 40, bottom: 40 },
      tooltip: {
        trigger: "axis",
        formatter(params) {
          const i = params[0].dataIndex;
          const rows = data.series.map((s) => {
            if (s.total[i] === null) return "";
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

  controls.stance.addEventListener("change", load);
  controls.quality.addEventListener("change", load);
  controls.smoothed.addEventListener("change", draw);
  applyURL();
  load().catch((error) => { el.replaceWith(Object.assign(document.createElement("p"), {
    className: "msg msg-error", textContent: error.message })); });
})();
