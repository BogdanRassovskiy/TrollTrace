// Демонстрационный график: проверка, что ECharts работает в canvas-режиме.
// Реальные графики (Этап 8) берут данные из агрегатной таблицы через JSON API.
(function () {
  const el = document.getElementById("demo-chart");
  if (!el || !window.echarts) return;

  const chart = echarts.init(el, null, { renderer: "canvas" });
  const weeks = Array.from({ length: 26 }, (_, i) => `W${i + 1}`);
  const series = ["Слой A", "Слой B", "Слой D"].map((name, s) => ({
    name,
    type: "line",
    showSymbol: false,
    data: weeks.map((_, i) => +(Math.max(0, Math.sin((i + s * 4) / 4) * 0.2 + 0.25)).toFixed(3)),
  }));

  chart.setOption({
    animation: false,
    tooltip: { trigger: "axis", valueFormatter: (v) => `${(v * 100).toFixed(1)} %` },
    legend: { top: 0 },
    grid: { left: 48, right: 16, top: 36, bottom: 32 },
    xAxis: { type: "category", data: weeks },
    yAxis: { type: "value", axisLabel: { formatter: (v) => `${Math.round(v * 100)} %` } },
    series,
  });

  window.addEventListener("resize", () => chart.resize());
})();
