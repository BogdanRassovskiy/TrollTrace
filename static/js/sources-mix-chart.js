// Общая гистограмма источников (README §4.2, §4.3): каждый столбец — период сбора (день,
// неделя или месяц); сегменты — источники пропорционально числу документов, цвет — средняя
// оценка качества. Два режима: «podíl» — каждый столбец 100 % (состав периода), «počet» —
// абсолютное число документов (периоды сравниваются между собой).
(function () {
  const all = JSON.parse(document.getElementById("sources-mix")?.textContent || "null");
  const el = document.getElementById("mix-chart");
  if (!all || !el || !window.echarts) return;

  // Те же цвета оценок, что на странице источника; между ними — линейная интерполяция.
  const STOPS = { 1: [198, 40, 40], 2: [245, 124, 0], 3: [251, 192, 45], 4: [124, 179, 66], 5: [46, 125, 50] };
  const NO_SCORE = [154, 165, 177];
  const css = getComputedStyle(document.documentElement);
  const surface = css.getPropertyValue("--surface").trim() || "#fff";
  const text = css.getPropertyValue("--text").trim() || "#1c1d21";

  function qualityRgb(mean) {
    if (mean == null) return NO_SCORE;
    const q = Math.min(5, Math.max(1, mean));
    const lo = Math.floor(q);
    const hi = Math.min(5, lo + 1);
    const t = q - lo;
    return STOPS[lo].map((c, i) => Math.round(c + (STOPS[hi][i] - c) * t));
  }
  const toCss = (rgb) => `rgb(${rgb.join(",")})`;
  const labelColor = (rgb) => (0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2] > 150 ? "#1c1d21" : "#fff");
  const grid = { left: 48, right: 16, top: 28, bottom: 32 };
  const chart = echarts.init(el, null, { renderer: "canvas" });
  // Выбор режима и периода запоминается в браузере (только удобство, без него всё работает).
  const state = { mode: "share", period: "day" };
  const allowed = { mode: ["share", "count"], period: Object.keys(all) };
  for (const key of Object.keys(state)) {
    try {
      const saved = localStorage.getItem(`sources-mix-${key}`);
      if (allowed[key].includes(saved)) state[key] = saved;
    } catch (e) {}
  }
  const fmtNumber = (v) => v.toLocaleString("cs-CZ");

  // Округление максимума оси вверх до «красивого» числа (1; 1,5; 2; 2,5; 3; 4; 5; 6; 8 × 10^k), чтобы знать
  // реальную высоту сегментов в пикселях и решать, помещается ли подпись.
  function niceMax(value) {
    if (value <= 0) return 1;
    const step = Math.pow(10, Math.floor(Math.log10(value)));
    for (const k of [1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]) if (k * step >= value) return k * step;
    return 10 * step;
  }

  function build() {
    const data = all[state.period];
    const plotHeight = chart.getHeight() - grid.top - grid.bottom;
    // Широкие столбцы (недели, месяцы) подписываются горизонтально, узкие — вертикально.
    const barWidth = ((chart.getWidth() - grid.left - grid.right) / data.dates.length) * 0.82;
    const horizontal = barWidth >= 70;
    const isShare = state.mode === "share";
    const axisMax = isShare ? 100 : niceMax(Math.max(...data.totals));
    const series = data.sources.map((source) => ({
      name: source.name,
      type: "bar",
      stack: "mix",
      barCategoryGap: "18%",
      cursor: "pointer",
      emphasis: { focus: "series" },
      data: source.counts.map((n, i) => {
        const total = data.totals[i];
        const share = total ? (n / total) * 100 : 0;
        const rgb = qualityRgb(source.quality[i]);
        const value = isShare ? share : n;
        const height = (value / axisMax) * plotHeight;
        return {
          value,
          share,
          n,
          quality: source.quality[i],
          itemStyle: { color: toCss(rgb), borderColor: surface, borderWidth: n ? 1 : 0 },
          // Название внутри сегмента — только если оно туда помещается хотя бы частично.
          label: {
            show: horizontal ? height >= 16 : height >= 36,
            position: "inside",
            rotate: horizontal ? 0 : 90,
            width: Math.max(0, (horizontal ? barWidth : height) - 6),
            overflow: "truncate",
            fontSize: 11,
            color: labelColor(rgb),
            formatter: () => source.name,
          },
        };
      }),
    }));
    chart.setOption(
      {
        animation: false,
        grid,
        tooltip: {
          trigger: "item",
          formatter: (p) => {
            const d = p.data;
            const quality = d.quality == null ? "bez hodnocení" : d.quality.toFixed(2).replace(".", ",");
            return (
              `<strong>${echarts.format.encodeHTML(p.seriesName)}</strong><br>` +
              `${data.titles[p.dataIndex]}: ${fmtNumber(d.n)} z ${fmtNumber(data.totals[p.dataIndex])} dokumentů ` +
              `(${Math.round(d.share)} %)<br>Prům. hodnocení: ${quality}`
            );
          },
        },
        xAxis: [
          { type: "category", data: data.labels },
          // Верхняя ось — число документов за период (в режиме долей столбец всегда 100 %).
          {
            type: "category",
            position: "top",
            data: data.dates,
            axisLine: { show: false },
            axisTick: { show: false },
            axisLabel: { interval: 0, fontSize: 11, color: text, formatter: (v, i) => (data.totals[i] ? fmtNumber(data.totals[i]) : "") },
          },
        ],
        yAxis: isShare
          ? { type: "value", min: 0, max: 100, interval: 25, axisLabel: { formatter: "{value} %" } }
          : {
              type: "value",
              min: 0,
              max: axisMax,
              minInterval: 1,
              axisLabel: { formatter: fmtNumber },
            },
        series,
      },
      true,
    );
  }

  // Кнопки переключателей: data-mix-mode="share|count", data-mix-period="day|week|month".
  const buttons = document.querySelectorAll("[data-mix-mode], [data-mix-period]");
  const keyOf = (b) => ("mixMode" in b.dataset ? "mode" : "period");
  const valueOf = (b) => b.dataset.mixMode ?? b.dataset.mixPeriod;
  function syncButtons() {
    buttons.forEach((b) => {
      const active = state[keyOf(b)] === valueOf(b);
      b.classList.toggle("active", active);
      b.setAttribute("aria-pressed", String(active));
    });
  }
  buttons.forEach((b) =>
    b.addEventListener("click", () => {
      const key = keyOf(b);
      state[key] = valueOf(b);
      try {
        localStorage.setItem(`sources-mix-${key}`, state[key]);
      } catch (e) {}
      syncButtons();
      build();
    }),
  );
  syncButtons();
  build();
  chart.on("click", (p) => {
    const source = all[state.period].sources[p.seriesIndex];
    if (source) window.location.href = source.url;
  });
  window.addEventListener("resize", () => {
    chart.resize();
    build();
  });
})();
