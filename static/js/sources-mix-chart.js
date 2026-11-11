// Общая гистограмма источников (README §4.2, §4.3): каждый столбец — день сбора; сегменты —
// источники пропорционально числу документов, цвет — средняя оценка качества.
// Два режима: «podíl» — каждый столбец 100 % (состав дня), «počet» — абсолютное число
// документов (дни сравниваются между собой).
(function () {
  const data = JSON.parse(document.getElementById("sources-mix")?.textContent || "null");
  const el = document.getElementById("mix-chart");
  if (!data || !el || !window.echarts) return;

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
  const fmtDate = (iso) => iso.slice(8, 10) + "." + iso.slice(5, 7) + ".";
  const grid = { left: 48, right: 16, top: 28, bottom: 32 };
  const chart = echarts.init(el, null, { renderer: "canvas" });
  const STORAGE_KEY = "sources-mix-mode";
  let mode = "share";
  try {
    if (localStorage.getItem(STORAGE_KEY) === "count") mode = "count";
  } catch (e) {}

  // Округление максимума оси вверх до «красивого» числа (1, 2, 5 × 10^k), чтобы знать
  // реальную высоту сегментов в пикселях и решать, помещается ли подпись.
  function niceMax(value) {
    if (value <= 0) return 1;
    const step = Math.pow(10, Math.floor(Math.log10(value)));
    for (const k of [1, 2, 2.5, 5, 10]) if (k * step >= value) return k * step;
    return 10 * step;
  }

  function build() {
    const plotHeight = chart.getHeight() - grid.top - grid.bottom;
    const isShare = mode === "share";
    const axisMax = isShare ? 100 : niceMax(Math.max(...data.totals));
    const series = data.sources.map((source) => ({
      name: source.name,
      type: "bar",
      stack: "mix",
      barCategoryGap: "18%",
      cursor: "pointer",
      emphasis: { focus: "series" },
      data: source.counts.map((n, day) => {
        const total = data.totals[day];
        const share = total ? (n / total) * 100 : 0;
        const rgb = qualityRgb(source.quality[day]);
        const value = isShare ? share : n;
        const height = (value / axisMax) * plotHeight;
        return {
          value,
          share,
          n,
          quality: source.quality[day],
          itemStyle: { color: toCss(rgb), borderColor: surface, borderWidth: n ? 1 : 0 },
          // Название внутри сегмента — только если оно туда помещается хотя бы частично.
          label: {
            show: height >= 36,
            position: "inside",
            rotate: 90,
            width: Math.max(0, height - 6),
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
              `${fmtDate(data.dates[p.dataIndex])}: ${d.n} z ${data.totals[p.dataIndex]} dokumentů ` +
              `(${Math.round(d.share)} %)<br>Prům. hodnocení: ${quality}`
            );
          },
        },
        xAxis: [
          { type: "category", data: data.dates, axisLabel: { formatter: fmtDate } },
          // Верхняя ось — число документов за день (в режиме долей столбец всегда 100 %).
          {
            type: "category",
            position: "top",
            data: data.dates,
            axisLine: { show: false },
            axisTick: { show: false },
            axisLabel: { interval: 0, fontSize: 11, color: text, formatter: (v, i) => (data.totals[i] ? String(data.totals[i]) : "") },
          },
        ],
        yAxis: isShare
          ? { type: "value", min: 0, max: 100, interval: 25, axisLabel: { formatter: "{value} %" } }
          : {
              type: "value",
              min: 0,
              max: axisMax,
              minInterval: 1,
              axisLabel: { formatter: (v) => v.toLocaleString("cs-CZ") },
            },
        series,
      },
      true,
    );
  }

  const buttons = document.querySelectorAll("[data-mix-mode]");
  function syncButtons() {
    buttons.forEach((b) => {
      const active = b.dataset.mixMode === mode;
      b.classList.toggle("active", active);
      b.setAttribute("aria-pressed", String(active));
    });
  }
  buttons.forEach((b) =>
    b.addEventListener("click", () => {
      mode = b.dataset.mixMode;
      try {
        localStorage.setItem(STORAGE_KEY, mode);
      } catch (e) {}
      syncButtons();
      build();
    }),
  );
  syncButtons();
  build();
  chart.on("click", (p) => {
    const source = data.sources[p.seriesIndex];
    if (source) window.location.href = source.url;
  });
  window.addEventListener("resize", () => {
    chart.resize();
    build();
  });
})();
