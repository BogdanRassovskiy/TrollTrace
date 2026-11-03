// Графики страницы источника (README §4.2, §4.3): объём и оценка качества по дням.
(function () {
  const data = JSON.parse(document.getElementById("source-series")?.textContent || "null");
  if (!data || !window.echarts) return;
  const charts = [];
  const base = {
    animation: false,
    tooltip: { trigger: "axis" },
    legend: { top: 0 },
    grid: { left: 48, right: 48, top: 36, bottom: 32 },
    xAxis: { type: "category", data: data.dates },
  };

  const volumeEl = document.getElementById("volume-chart");
  if (volumeEl) {
    const chart = echarts.init(volumeEl, null, { renderer: "canvas" });
    chart.setOption({
      ...base,
      yAxis: { type: "value", minInterval: 1 },
      series: [
        { name: "Všechny dokumenty", type: "bar", data: data.volume, itemStyle: { color: "#9aa5b1" } },
        { name: "Ve sledovaných rubrikách", type: "bar", data: data.in_scope, itemStyle: { color: "#2f6fb0" } },
      ],
    });
    charts.push(chart);
  }

  const qualityEl = document.getElementById("quality-chart");
  if (qualityEl) {
    const colors = { 5: "#2e7d32", 4: "#7cb342", 3: "#fbc02d", 2: "#f57c00", 1: "#c62828" };
    const chart = echarts.init(qualityEl, null, { renderer: "canvas" });
    chart.setOption({
      ...base,
      yAxis: [
        { type: "value", name: "dokumenty", minInterval: 1 },
        { type: "value", name: "průměr", min: 1, max: 5 },
      ],
      series: [
        ...[5, 4, 3, 2, 1].map((score) => ({
          name: `Hodnocení ${score}`,
          type: "bar",
          stack: "scores",
          data: data.scores[score],
          itemStyle: { color: colors[score] },
        })),
        {
          name: "Průměr",
          type: "line",
          yAxisIndex: 1,
          data: data.mean_quality,
          connectNulls: false,
          itemStyle: { color: "#263238" },
        },
      ],
    });
    charts.push(chart);
  }

  window.addEventListener("resize", () => charts.forEach((c) => c.resize()));
})();
