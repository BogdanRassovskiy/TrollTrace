// Режим сравнения (README §7.1): все акторы по одному нарративу, до ~350 линий на canvas.
//
// Линии тонкие и полупрозрачные, цвет по слою или партии. Наведение подсвечивает ближайшую
// линию и приглушает остальные; закреплённые (список справа или клик по линии) рисуются
// поверх. Поверх фона — медиана и межквартильный размах групп. Клик по точке открывает посты,
// если линий не больше N; иначе — только у закреплённой линии. Тепловая карта «актор × неделя»:
// клик по ячейке однозначен и работает всегда.
(function () {
  const TT = window.TT;
  const { config, getJSON, percent, weekLabel, escapeHtml } = TT;
  const el = document.getElementById("compare-chart");
  if (!el || !window.echarts) return;
  const chart = echarts.init(el, null, { renderer: "canvas" });
  TT.onResize(chart);

  const $ = (id) => document.getElementById(id);
  const controls = {
    narrative: $("narrative"), stance: $("stance"), quality: $("min-quality"), party: $("party"),
    colorBy: $("color-by"), groupLines: $("group-lines"), sort: $("sort"), search: $("legend-search"),
  };
  const HOVER_PX = 12;
  const state = { data: null, actors: [], pinned: new Set(), view: "lines", hovered: null };

  const status = (text) => { $("status").textContent = text; };
  const colorOf = (s) => (controls.colorBy.value === "party" ? TT.partyColor(s.party) : TT.layerColor(s.layer));
  const groupOf = (s) => (controls.colorBy.value === "party" ? s.party || "bez strany" : s.layer);
  const layers = () => [...document.querySelectorAll("#layers input:checked")].map((i) => i.value);

  async function load() {
    status("Načítám…");
    chart.showLoading({ text: "" });
    try {
      state.data = await getJSON("/api/series/", {
        narrative: controls.narrative.value, stance: controls.stance.value,
        min_quality: controls.quality.value, party: controls.party.value, layer: layers(), groups: "0",
      });
    } catch (error) {
      status(error.message);
      return;
    } finally {
      chart.hideLoading();
    }
    state.actors = state.data.series.filter((s) => s.kind === "actor");
    const keys = new Set(state.actors.map((s) => s.key));
    state.pinned = new Set([...state.pinned].filter((k) => keys.has(k)));
    const url = new URL(window.location);
    url.searchParams.set("narrative", controls.narrative.value);
    window.history.replaceState(null, "", url);
    renderLegend();
    draw();
  }

  // --- Линии ----------------------------------------------------------------------------

  function quantile(sorted, q) {
    const pos = (sorted.length - 1) * q;
    const lo = Math.floor(pos);
    const hi = Math.ceil(pos);
    return sorted[lo] + (sorted[hi] - sorted[lo]) * (pos - lo);
  }

  function groupSeries() {
    const groups = new Map();
    state.actors.forEach((s) => {
      const key = groupOf(s);
      if (!groups.has(key)) groups.set(key, { color: colorOf(s), members: [] });
      groups.get(key).members.push(s);
    });
    const out = [];
    for (const [name, group] of groups) {
      if (group.members.length < 3) continue; // medián ze dvou čar nic neříká
      const q1 = [], med = [], spread = [];
      state.data.weeks.forEach((_, i) => {
        const values = group.members.map((s) => s.share[i]).filter((v) => v !== null).sort((a, b) => a - b);
        if (values.length < 3) { q1.push(null); med.push(null); spread.push(null); return; }
        const a = quantile(values, 0.25);
        q1.push(a); med.push(quantile(values, 0.5)); spread.push(quantile(values, 0.75) - a);
      });
      const stack = `iqr:${name}`;
      const common = { type: "line", silent: true, symbol: "none", connectNulls: false, z: 3, tooltip: { show: false } };
      out.push({ ...common, id: `${stack}:lo`, stack, data: q1, lineStyle: { opacity: 0 } });
      out.push({ ...common, id: `${stack}:band`, stack, data: spread, lineStyle: { opacity: 0 },
        areaStyle: { color: group.color, opacity: 0.12 } });
      out.push({ ...common, id: `median:${name}`, name: `Medián ${name}`, data: med, z: 6,
        lineStyle: { color: group.color, width: 3, type: "dashed" } });
    }
    return out;
  }

  function lineOptions() {
    const many = state.actors.length > 60;
    const series = state.actors.map((s) => {
      const pinned = state.pinned.has(s.key);
      return {
        id: s.key, name: s.name, type: "line", data: s.share, connectNulls: false,
        showSymbol: pinned, symbolSize: 5, z: pinned ? 10 : 2, color: colorOf(s),
        lineStyle: { width: pinned ? 2.5 : 1, opacity: pinned ? 1 : many ? 0.25 : 0.55 },
        emphasis: { focus: "series", lineStyle: { width: 3, opacity: 1 } },
        blur: { lineStyle: { opacity: 0.04 } },
        endLabel: { show: pinned, formatter: s.name, fontSize: 11 },
      };
    });
    if (controls.groupLines.checked) series.push(...groupSeries());
    return {
      animation: false,
      grid: { left: 56, right: 120, top: 16, bottom: 40 },
      tooltip: { show: false },
      xAxis: { type: "category", data: state.data.weeks.map(weekLabel), boundaryGap: false },
      yAxis: { type: "value", min: 0, axisLabel: { formatter: (v) => percent(v, 0) } },
      series,
    };
  }

  // Ближайшая линия к курсору в той же неделе (по пикселям); candidates — среди каких искать.
  function nearest(offsetX, offsetY, candidates) {
    if (!chart.containPixel({ gridIndex: 0 }, [offsetX, offsetY])) return null;
    const index = Math.round(chart.convertFromPixel({ gridIndex: 0 }, [offsetX, offsetY])[0]);
    if (index < 0 || index >= state.data.weeks.length) return null;
    let best = null;
    for (const s of candidates) {
      const value = s.share[index];
      if (value === null) continue;
      const y = chart.convertToPixel({ gridIndex: 0 }, [index, value])[1];
      const distance = Math.abs(y - offsetY);
      if (distance <= HOVER_PX && (!best || distance < best.distance)) best = { s, index, distance };
    }
    return best;
  }

  const tip = Object.assign(document.createElement("div"), { className: "chart-tip", hidden: true });
  el.style.position = "relative";
  el.appendChild(tip);

  function showTip(hit, x, y) {
    const { s, index } = hit;
    tip.innerHTML = `<b>${escapeHtml(s.name)}</b> <span class="muted">${escapeHtml(s.layer)}${s.party ? ` · ${escapeHtml(s.party)}` : ""}</span><br>
      Týden od ${weekLabel(state.data.weeks[index])}: <b>${percent(s.share[index])}</b>
      (${s.n[index]} z ${s.total[index]})${s.z[index] !== null ? ` · z ${s.z[index].toFixed(1)}` : ""}`;
    tip.hidden = false;
    tip.style.left = `${Math.min(x + 14, el.clientWidth - 240)}px`;
    tip.style.top = `${Math.max(y - 56, 0)}px`;
  }

  function setHovered(key) {
    if (state.hovered === key) return;
    if (state.hovered) chart.dispatchAction({ type: "downplay", seriesId: state.hovered });
    state.hovered = key;
    if (key) chart.dispatchAction({ type: "highlight", seriesId: key });
  }

  let frame = null;
  chart.getZr().on("mousemove", (event) => {
    if (state.view !== "lines" || !state.data) return;
    cancelAnimationFrame(frame);
    frame = requestAnimationFrame(() => {
      const hit = nearest(event.offsetX, event.offsetY, state.actors);
      setHovered(hit ? hit.s.key : null);
      if (hit) showTip(hit, event.offsetX, event.offsetY); else tip.hidden = true;
    });
  });
  chart.getZr().on("globalout", () => { setHovered(null); tip.hidden = true; });

  chart.getZr().on("click", (event) => {
    if (state.view !== "lines" || !state.data) return;
    const limit = config.click_max_lines;
    const direct = state.actors.length <= limit;
    if (direct) {
      const hit = nearest(event.offsetX, event.offsetY, state.actors);
      if (hit) openPosts(hit.s, hit.index);
      return;
    }
    const pinned = state.actors.filter((s) => state.pinned.has(s.key));
    const onPinned = nearest(event.offsetX, event.offsetY, pinned);
    if (onPinned) { openPosts(onPinned.s, onPinned.index); return; }
    const hit = nearest(event.offsetX, event.offsetY, state.actors);
    if (hit) {
      togglePin(hit.s.key);
      status(`Čára „${hit.s.name}“ připnuta. Další kliknutí na její bod otevře příspěvky.`);
    } else {
      status(`V grafu je ${state.actors.length} čar (víc než ${limit}): pro příspěvky nejdřív připněte čáru — kliknutím na ni nebo v seznamu.`);
    }
  });

  // --- Тепловая карта -------------------------------------------------------------------

  function sortedActors() {
    const firstSeen = (s) => { const i = s.n.findIndex((n) => n > 0); return i < 0 ? Infinity : i; };
    const byName = (a, b) => a.name.localeCompare(b.name, "cs");
    const sorters = {
      first: (a, b) => firstSeen(a) - firstSeen(b) || byName(a, b),
      layer: (a, b) => a.layer.localeCompare(b.layer) || byName(a, b),
      party: (a, b) => (a.party || "~").localeCompare(b.party || "~", "cs") || byName(a, b),
      name: byName,
    };
    return [...state.actors].sort(sorters[controls.sort.value]);
  }

  function heatmapOptions() {
    const rows = sortedActors().reverse(); // první nahoře
    const cells = [];
    let max = 0;
    rows.forEach((s, y) => s.share.forEach((v, x) => {
      if (v === null) return;
      cells.push([x, y, v]);
      max = Math.max(max, v);
    }));
    state.heatRows = rows;
    const height = Math.max(360, rows.length * 12 + 100);
    el.style.height = `${height}px`;
    chart.resize();
    return {
      animation: false,
      grid: { left: rows.length <= 80 ? 180 : 24, right: 24, top: 16, bottom: 72 },
      tooltip: {
        formatter(p) {
          const s = rows[p.value[1]];
          const i = p.value[0];
          return `<b>${escapeHtml(s.name)}</b> (${escapeHtml(s.layer)}${s.party ? `, ${escapeHtml(s.party)}` : ""})<br>
            Týden od ${weekLabel(state.data.weeks[i])}: <b>${percent(s.share[i])}</b> (${s.n[i]} z ${s.total[i]})`;
        },
      },
      xAxis: { type: "category", data: state.data.weeks.map(weekLabel), splitArea: { show: false } },
      yAxis: { type: "category", data: rows.map((s) => s.name),
        axisLabel: { show: rows.length <= 80, fontSize: 11 } },
      visualMap: { min: 0, max: max || 1, calculable: true, orient: "horizontal", left: "center",
        bottom: 8, inRange: { color: ["#f1f3f8", "#f6c28b", "#d1372e", "#5b0a0a"] },
        formatter: (v) => percent(v, 0) },
      series: [{ type: "heatmap", data: cells, progressive: 0, emphasis: { itemStyle: { borderColor: "#000", borderWidth: 1 } } }],
    };
  }

  chart.on("click", (params) => {
    if (state.view !== "heatmap" || params.seriesType !== "heatmap") return;
    openPosts(state.heatRows[params.value[1]], params.value[0]);
  });

  // --- Общее ----------------------------------------------------------------------------

  function openPosts(s, index) {
    TT.showPosts(
      { narrative: controls.narrative.value, actor: s.id, week: state.data.weeks[index],
        min_quality: controls.quality.value, stance: controls.stance.value },
      `${s.name} · týden od ${weekLabel(state.data.weeks[index])}`,
    );
  }

  function draw() {
    if (!state.data) return;
    setHovered(null);
    tip.hidden = true;
    if (!state.actors.length) {
      chart.clear();
      status("Pro zvolený filtr nejsou žádná data.");
      return;
    }
    if (state.view === "heatmap") {
      chart.setOption(heatmapOptions(), true);
    } else {
      el.style.height = "";
      chart.resize();
      chart.setOption(lineOptions(), true);
    }
    const direct = state.actors.length <= config.click_max_lines;
    status(`${state.actors.length} aktérů · ${state.data.weeks.length} týdnů · připnuto ${state.pinned.size}. ` +
      (state.view === "heatmap" ? "Kliknutím na buňku otevřete příspěvky."
        : direct ? "Kliknutím na bod otevřete příspěvky."
          : "Kliknutím čáru připnete; kliknutím na bod připnuté čáry otevřete příspěvky."));
  }

  function togglePin(key) {
    if (state.pinned.has(key)) state.pinned.delete(key); else state.pinned.add(key);
    renderLegend();
    draw();
  }

  function renderLegend() {
    const query = controls.search.value.trim().toLocaleLowerCase("cs");
    const items = [...state.actors]
      .filter((s) => !query || s.name.toLocaleLowerCase("cs").includes(query))
      .sort((a, b) => (state.pinned.has(b.key) - state.pinned.has(a.key)) || a.name.localeCompare(b.name, "cs"));
    $("legend-count").textContent = `${state.actors.length} aktérů`;
    $("legend").innerHTML = items.map((s) => `
      <li class="${state.pinned.has(s.key) ? "pinned" : ""}" data-key="${s.key}">
        <label><input type="checkbox" ${state.pinned.has(s.key) ? "checked" : ""}>
          <span class="swatch" style="background:${colorOf(s)}"></span>
          <span class="name" title="${escapeHtml(s.name)}">${escapeHtml(s.name)}</span></label>
        <span class="muted small">${escapeHtml(s.layer)}</span>
      </li>`).join("");
  }

  $("legend").addEventListener("change", (event) => {
    const key = event.target.closest("li")?.dataset.key;
    if (key) togglePin(key);
  });
  $("legend").addEventListener("mouseover", (event) => {
    const key = event.target.closest("li")?.dataset.key;
    if (key && state.view === "lines") setHovered(key);
  });
  $("legend").addEventListener("mouseleave", () => setHovered(null));
  $("unpin-all").addEventListener("click", () => { state.pinned.clear(); renderLegend(); draw(); });
  controls.search.addEventListener("input", renderLegend);

  document.querySelectorAll(".toggle-btn").forEach((button) => button.addEventListener("click", () => {
    document.querySelectorAll(".toggle-btn").forEach((b) => b.classList.toggle("active", b === button));
    state.view = button.dataset.view;
    $("sort-wrap").hidden = state.view !== "heatmap";
    controls.groupLines.parentElement.hidden = state.view === "heatmap";
    draw();
  }));

  [controls.narrative, controls.stance, controls.quality, controls.party].forEach((c) => c.addEventListener("change", load));
  $("layers").addEventListener("change", load);
  [controls.colorBy, controls.groupLines, controls.sort].forEach((c) => c.addEventListener("change", () => { renderLegend(); draw(); }));

  load();
})();
