// Карточка нарратива: доля по слоям во времени (группы из агрегатной таблицы).
(function () {
  const { config, getJSON, percent, weekLabel, escapeHtml, layerColor, onResize } = window.TT;
  const el = document.getElementById("narrative-chart");
  if (!el || !window.echarts) return;
  const chart = echarts.init(el, null, { renderer: "canvas" });
  onResize(chart);
  getJSON("/api/series/", { narrative: config.narrative, min_quality: config.min_quality, groups: "only" })
    .then((data) => {
      const series = data.series.filter((s) => s.key.startsWith("layer:") || s.key === "all");
      chart.setOption({
        animation: false,
        legend: { top: 0 },
        grid: { left: 56, right: 24, top: 36, bottom: 40 },
        tooltip: {
          trigger: "axis",
          formatter(params) {
            const i = params[0].dataIndex;
            return `<b>Týden od ${weekLabel(data.weeks[i])}</b>` + series.map((s) => (s.total[i] === null ? ""
              : `<div>${escapeHtml(label(s))}: <b>${percent(s.share[i])}</b> (${s.n[i]} z ${s.total[i]})</div>`)).join("");
          },
        },
        xAxis: { type: "category", data: data.weeks.map(weekLabel) },
        yAxis: { type: "value", min: 0, axisLabel: { formatter: (v) => percent(v, 0) } },
        series: series.map((s) => ({
          name: label(s), type: "line", data: s.share, connectNulls: false,
          color: s.key === "all" ? "#263238" : layerColor(s.key.slice(6)),
          lineStyle: { width: s.key === "all" ? 3 : 1.5 },
        })),
      });
    })
    .catch((error) => { el.textContent = error.message; });

  function label(s) {
    return s.key === "all" ? "Všechny vrstvy (bez Institucí bezpečnosti)" : window.TT.layerName(s.key.slice(6));
  }
})();
