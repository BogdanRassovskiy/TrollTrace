// Нарративы друг против друга: недельная доля каждого нарратива по выбранным акторам.
//
// Данные — /api/narrative-shares/ (агрегатная таблица). Нарративы включаются и выключаются на
// клиенте без запроса; смена акторов, слоёв, позиции или порога качества — новый запрос.
// Режимы: «podíl z dokumentů» — линии, общий знаменатель (все документы выбранных акторов);
// «poměr mezi narativy» — 100 % стопка, доля нарратива среди включённых нарративов.
// Наведение подсвечивает ближайшую линию (или полосу стопки), клик открывает её документы.
(function () {
  const { config, getJSON, percent, weekLabel, escapeHtml, layerColor, onResize } = window.TT;
  const el = document.getElementById("race-chart");
  if (!el || !window.echarts) return;
  const chart = echarts.init(el, null, { renderer: "canvas" });
  onResize(chart);

  const $ = (id) => document.getElementById(id);
  const HOVER_PX = 12;
  const ALL_LAYERS = Object.keys(config.layers);
  // Цвета: самым частым нарративам — контрастная палитра, остальным — оттенки «золотым углом».
  // Назначаются один раз по первой загрузке, дальше не меняются (цвет = нарратив).
  const DISTINCT = ["#c62828", "#1565c0", "#2e7d32", "#ef6c00", "#6a1b9a", "#00838f", "#ad1457",
    "#f9a825", "#4e342e", "#283593", "#558b2f", "#37474f"];
  const COLORS = {};
  function assignColors(series) {
    if (Object.keys(COLORS).length) return;
    const total = (s) => s.n.reduce((a, b) => a + b, 0);
    const ranked = [...series].sort((a, b) => total(b) - total(a)).map((s) => s.code);
    const rest = config.narratives.map((n) => n.code).filter((c) => !ranked.includes(c));
    [...ranked, ...rest].forEach((code, i) => {
      COLORS[code] = DISTINCT[i] || `hsl(${Math.round((i * 137.508) % 360)}, 55%, ${i % 2 ? 40 : 52}%)`;
    });
  }

  const state = {
    data: null, stance: "all", mode: "share", tab: "narratives", hovered: null,
    narrativesOff: new Set(), actorsOff: new Set(), topicsOff: new Set(),
  };

  function status(text) { $("status").textContent = text; }

  function layers() {
    return [...$("layers").querySelectorAll("input:checked")].map((i) => i.value);
  }

  function actorsInLayers() {
    const chosen = new Set(layers());
    return config.actors.filter((a) => chosen.has(a.layer));
  }

  // Выбор акторов для API: слои плюс более короткий из списков «только эти» / «кроме этих».
  function actorParams() {
    const chosen = layers();
    const params = { layer: chosen.length === ALL_LAYERS.length ? [] : chosen };
    const pool = actorsInLayers();
    const off = pool.filter((a) => state.actorsOff.has(a.id)).map((a) => a.id);
    const on = pool.filter((a) => !state.actorsOff.has(a.id)).map((a) => a.id);
    if (!off.length) return params;
    if (on.length <= off.length) params.actor = on.length ? on : [0];
    else params.exclude = off;
    return params;
  }

  async function load() {
    status("Načítám…");
    try {
      state.data = await getJSON("/api/narrative-shares/", {
        min_quality: $("min-quality").value, stance: state.stance, ...actorParams(),
      });
      assignColors(state.data.series);
      draw();
    } catch (error) {
      state.data = null;
      chart.clear();
      status(error.message);
    }
    renderList();
  }

  // narativ je na grafu, když je zaškrtnutý on sám i jeho téma (téma = filtr nad výběrem)
  const TOPIC_OF = Object.fromEntries(config.narratives.map((n) => [n.code, n.topic]));
  function shown(code) {
    return !state.narrativesOff.has(code) && !state.topicsOff.has(TOPIC_OF[code]);
  }

  function visible() {
    return state.data ? state.data.series.filter((s) => shown(s.code)) : [];
  }

  function numerator(s) { return state.stance === "all" ? s.n : s[state.stance]; }

  // Значения по режиму: доля от всех документов или доля среди включённых нарративов.
  function values(series) {
    if (state.mode === "share") return series.map((s) => s.share);
    const weeks = state.data.weeks.length;
    const sums = Array.from({ length: weeks }, (_, i) => series.reduce((acc, s) => acc + numerator(s)[i], 0));
    return series.map((s) => numerator(s).map((n, i) => (sums[i] ? round(n / sums[i]) : null)));
  }

  function round(x) { return Math.round(x * 10000) / 10000; }

  function draw() {
    if (!state.data) return;
    syncURL();
    const series = visible();
    state.values = values(series);
    state.series = series;
    const stacked = state.mode === "mix";
    tip.hidden = true;
    state.hovered = null;
    chart.setOption({
      animation: false,
      grid: { left: 56, right: 24, top: 16, bottom: 40 },
      tooltip: { show: false },
      xAxis: { type: "category", data: state.data.weeks.map(weekLabel), boundaryGap: false },
      yAxis: { type: "value", min: 0, max: stacked ? 1 : null, axisLabel: { formatter: (v) => percent(v, 0) } },
      series: series.map((s, k) => ({
        id: s.code, name: s.name, type: "line", data: state.values[k], color: COLORS[s.code],
        stack: stacked ? "mix" : undefined, areaStyle: stacked ? { opacity: 0.85 } : undefined,
        showSymbol: false, symbolSize: 5, connectNulls: false,
        lineStyle: { width: stacked ? 0.5 : 1.8, type: s.kind === "theme" ? "dashed" : "solid" },
        emphasis: { focus: "series", lineStyle: { width: stacked ? 1 : 3 } },
        blur: { lineStyle: { opacity: 0.15 }, areaStyle: { opacity: 0.2 } },
      })),
    }, true);
    const weeks = state.data.weeks.length;
    status(`${series.length} z ${state.data.series.length} narativů · ${state.data.actors} aktérů · ${weeks} týdnů. ` +
      (weeks ? "Kliknutím na čáru otevřete dokumenty." : "Pro tento výběr nejsou žádná data."));
  }

  // --- Наведение и клик --------------------------------------------------------------------

  function weekIndex(x, y) {
    if (!state.data || !chart.containPixel({ gridIndex: 0 }, [x, y])) return null;
    const i = Math.round(chart.convertFromPixel({ gridIndex: 0 }, [x, y])[0]);
    return i >= 0 && i < state.data.weeks.length ? i : null;
  }

  function hitAt(x, y) {
    const i = weekIndex(x, y);
    if (i === null) return null;
    const px = (v) => chart.convertToPixel({ gridIndex: 0 }, [i, v])[1];
    if (state.mode === "mix") {
      let base = 0;
      for (let k = 0; k < state.series.length; k++) {
        const v = state.values[k][i] || 0;
        if (v && y <= px(base) && y >= px(base + v)) return { k, i };
        base += v;
      }
      return null;
    }
    let best = null;
    state.series.forEach((s, k) => {
      const v = state.values[k][i];
      if (v === null) return;
      const d = Math.abs(px(v) - y);
      if (d <= HOVER_PX && (!best || d < best.d)) best = { k, i, d };
    });
    return best;
  }

  const tip = Object.assign(document.createElement("div"), { className: "chart-tip", hidden: true });
  el.style.position = "relative";
  el.appendChild(tip);

  function stanceBreakdown(s, i) {
    const counts = ["pro", "contra", "neutral"].map((k) => [k, s[k][i]]);
    const top = Math.max(...counts.map(([, n]) => n));
    if (!top) return "";
    return counts.map(([k, n]) => {
      const text = `${TT.STANCES[k]} ${n}`;
      return `<span class="stance-${k}">${n === top ? `<b>${text}</b>` : text}</span>`;
    }).join(" · ");
  }

  function showTip(hit, x, y) {
    const s = state.series[hit.k];
    const i = hit.i;
    const rank = state.values.map((v) => v[i] || 0).filter((v) => v > (state.values[hit.k][i] || 0)).length + 1;
    const breakdown = stanceBreakdown(s, i);
    tip.innerHTML = `<b>${escapeHtml(s.name)}</b>${s.kind === "theme" ? ' <span class="muted">téma</span>' : ""}<br>
      Týden od ${weekLabel(state.data.weeks[i])}: <b>${percent(state.values[hit.k][i])}</b>
      (${numerator(s)[i]} z ${state.data.total[i]} dok.) · ${rank}. místo
      ${breakdown ? `<br>Postoj: ${breakdown}` : ""}
      ${numerator(s)[i] ? '<br><span class="muted">Kliknutím otevřete dokumenty</span>' : ""}`;
    tip.hidden = false;
    tip.style.left = `${Math.min(x + 14, el.clientWidth - 250)}px`;
    tip.style.top = `${Math.max(y - 70, 0)}px`;
  }

  function setHovered(code) {
    if (state.hovered === code) return;
    if (state.hovered) chart.dispatchAction({ type: "downplay", seriesId: state.hovered });
    state.hovered = code;
    if (code) chart.dispatchAction({ type: "highlight", seriesId: code });
  }

  let frame = null;
  chart.getZr().on("mousemove", (event) => {
    cancelAnimationFrame(frame);
    frame = requestAnimationFrame(() => {
      const hit = hitAt(event.offsetX, event.offsetY);
      setHovered(hit ? state.series[hit.k].code : null);
      if (hit) showTip(hit, event.offsetX, event.offsetY); else tip.hidden = true;
    });
  });
  chart.getZr().on("globalout", () => { setHovered(null); tip.hidden = true; });

  chart.getZr().on("click", (event) => {
    const hit = hitAt(event.offsetX, event.offsetY);
    if (!hit) {
      if (state.data) status("Klikněte přímo na čáru — zvýrazněná čára a popisek ukazují, které dokumenty se otevřou.");
      return;
    }
    const s = state.series[hit.k];
    const week = state.data.weeks[hit.i];
    TT.showPosts({ narrative: s.code, week, group: "selection", min_quality: $("min-quality").value,
      stance: state.stance, ...actorParams() }, `${s.name} · týden od ${weekLabel(week)}`);
  });

  // --- Панель: нарративы и акторы ----------------------------------------------------------

  function query() { return $("list-search").value.trim().toLocaleLowerCase("cs"); }

  function listedNarratives(all) {
    const totals = Object.fromEntries((state.data ? state.data.series : []).map((s) =>
      [s.code, numerator(s).reduce((a, b) => a + b, 0)]));
    const q = all ? "" : query();
    return config.narratives
      .filter((n) => !q || n.name.toLocaleLowerCase("cs").includes(q) || n.code.includes(q))
      .map((n) => ({ ...n, total: totals[n.code] || 0 }))
      .sort((a, b) => b.total - a.total || a.name.localeCompare(b.name, "cs"));
  }

  function listedActors() {
    const q = query();
    return actorsInLayers().filter((a) => !q || a.name.toLocaleLowerCase("cs").includes(q));
  }

  function renderList() {
    const pool = actorsInLayers();
    $("count-narratives").textContent = `${config.narratives.filter((n) => shown(n.code)).length}/${config.narratives.length}`;
    $("count-actors").textContent = `${pool.filter((a) => !state.actorsOff.has(a.id)).length}/${pool.length}`;
    $("kind-filter").hidden = state.tab !== "narratives";
    document.querySelectorAll(".tab-btn").forEach((b) => {
      b.classList.toggle("active", b.dataset.tab === state.tab);
      b.setAttribute("aria-selected", String(b.dataset.tab === state.tab));
    });
    if (state.tab === "narratives") {
      // strom: téma → nadnarativ → narativ → podnarativ; sourozenci podle počtu dokumentů
      const list = $("pick-list");
      const open = list.classList.contains("tt-list") ? TT.openKeys(list) : undefined;
      const totals = Object.fromEntries(listedNarratives(true).map((n) => [n.code, n.total]));
      const tree = TT.buildTree(config.narratives);
      const sort = (items) => {
        items.sort((a, b) => (totals[b.code] || 0) - (totals[a.code] || 0) || a.name.localeCompare(b.name, "cs"));
        items.forEach((x) => sort(x.children));
      };
      tree.forEach((t) => sort(t.children));
      list.classList.add("tt-list", "tt-root");
      list.innerHTML = TT.treeHTML(tree, (n) => {
        const on = !state.narrativesOff.has(n.code);
        return `<label class="tt-check${shown(n.code) ? "" : " off"}" data-pick="${escapeHtml(n.code)}"><input type="checkbox" ${on ? "checked" : ""}>
            <span class="swatch" style="background:${COLORS[n.code] || "#9e9e9e"}"></span>
            <span class="name" title="${escapeHtml(n.name)}${n.children.length ? " — součet celé větve" : ""}">${escapeHtml(n.name)}</span>
            <span class="muted small" title="dokumentů za období${n.children.length ? " (celá větev, každý jednou)" : ""}">${n.kind === "theme" ? "téma · " : ""}${totals[n.code] || 0}</span></label>`;
      }, open, (t) => {
        const on = !state.topicsOff.has(t.topic);
        const count = config.narratives.filter((n) => n.topic === t.topic).length;
        return `<label class="tt-check tt-topic-check${on ? "" : " off"}" data-topic="${escapeHtml(t.topic)}"
            title="Téma jako filtr: odškrtnutím skryjete všechny jeho narativy, výběr uvnitř zůstane">
          <input type="checkbox" ${on ? "checked" : ""}> <span class="name">${escapeHtml(t.name)}</span>
          <span class="muted small">${count}</span></label>`;
      });
      TT.filterTree(list, $("list-search").value);
    } else {
      $("pick-list").classList.remove("tt-list", "tt-root");
      $("pick-list").innerHTML = listedActors().map((a) => {
        const on = !state.actorsOff.has(a.id);
        return `<li class="${on ? "" : "off"}" data-key="${a.id}">
          <label><input type="checkbox" ${on ? "checked" : ""}>
            <span class="swatch" style="background:${layerColor(a.layer)}"></span>
            <span class="name" title="${escapeHtml(a.name)}">${escapeHtml(a.name)}</span></label>
          <span class="muted small">${escapeHtml(TT.layerName(a.layer))}${a.party ? ` · ${escapeHtml(a.party)}` : ""}</span>
        </li>`;
      }).join("");
    }
  }

  // Изменение набора: нарративы перерисовываются без запроса, акторы — с новым запросом.
  function changed(reload) {
    renderList();
    if (reload) load(); else draw();
  }

  $("pick-list").addEventListener("change", (event) => {
    const topic = event.target.closest("[data-topic]")?.dataset.topic;
    if (topic) {
      if (event.target.checked) state.topicsOff.delete(topic); else state.topicsOff.add(topic);
      changed(false);
      return;
    }
    const key = state.tab === "narratives"
      ? event.target.closest("[data-pick]")?.dataset.pick
      : event.target.closest("li")?.dataset.key;
    if (!key) return;
    if (state.tab === "narratives") {
      if (event.target.checked) state.narrativesOff.delete(key); else state.narrativesOff.add(key);
      changed(false);
    } else {
      const id = Number(key);
      if (event.target.checked) state.actorsOff.delete(id); else state.actorsOff.add(id);
      changed(true);
    }
  });
  $("pick-list").addEventListener("mouseover", (event) => {
    const key = event.target.closest("[data-pick]")?.dataset.pick;
    if (state.tab === "narratives" && key && shown(key)) setHovered(key);
  });
  $("pick-list").addEventListener("mouseleave", () => setHovered(null));

  // «Vybrat všechny» / «zrušit výběr» действуют на видимые в списке (с учётом поиска).
  function setAll(on) {
    if (state.tab === "narratives") {
      listedNarratives().forEach((n) => (on ? state.narrativesOff.delete(n.code) : state.narrativesOff.add(n.code)));
      changed(false);
    } else {
      listedActors().forEach((a) => (on ? state.actorsOff.delete(a.id) : state.actorsOff.add(a.id)));
      changed(true);
    }
  }
  $("check-all").addEventListener("click", () => setAll(true));
  $("check-none").addEventListener("click", () => setAll(false));
  $("kind-filter").querySelectorAll("[data-kind]").forEach((button) => button.addEventListener("click", () => {
    config.narratives.forEach((n) => (n.kind === button.dataset.kind
      ? state.narrativesOff.delete(n.code) : state.narrativesOff.add(n.code)));
    changed(false);
  }));
  // Nadnarativ už obsahuje své narativy: výchozí výběr — jen nejvyšší úroveň stromu,
  // aby se větev nepočítala dvakrát.
  const ACTIVE = new Set(config.narratives.map((n) => n.code));
  const nested = () => config.narratives.filter((n) => n.parent && ACTIVE.has(n.parent)).map((n) => n.code);
  $("topics-all")?.addEventListener("click", () => { state.topicsOff = new Set(); changed(false); });
  $("topics-none")?.addEventListener("click", () => {
    state.topicsOff = new Set(Object.values(TOPIC_OF));
    changed(false);
  });
  $("top-level")?.addEventListener("click", () => {
    state.narrativesOff = new Set(nested());
    changed(false);
  });

  document.querySelectorAll(".tab-btn").forEach((button) => button.addEventListener("click", () => {
    state.tab = button.dataset.tab;
    $("list-search").value = "";
    renderList();
  }));
  $("list-search").addEventListener("input", renderList);

  // --- Переключатели и адрес ---------------------------------------------------------------

  function setStance(stance) {
    state.stance = stance;
    document.querySelectorAll("#stance .toggle-btn").forEach((b) => {
      const active = b.dataset.stance === stance;
      b.classList.toggle("active", active);
      b.setAttribute("aria-checked", String(active));
    });
  }
  document.querySelectorAll("#stance .toggle-btn").forEach((button) => button.addEventListener("click", () => {
    if (button.dataset.stance === state.stance) return;
    setStance(button.dataset.stance);
    load();
  }));

  function setMode(mode) {
    state.mode = mode;
    document.querySelectorAll(".mode-btn").forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
  }
  document.querySelectorAll(".mode-btn").forEach((button) => button.addEventListener("click", () => {
    setMode(button.dataset.mode);
    draw();
  }));

  $("min-quality").addEventListener("change", load);
  $("layers").addEventListener("change", () => { renderList(); load(); });

  function syncURL() {
    const chosen = layers();
    TT.syncURL({
      q: $("min-quality").value === String(config.min_quality) ? "" : $("min-quality").value,
      stance: state.stance === "all" ? "" : state.stance,
      mode: state.mode === "share" ? "" : state.mode,
      layers: chosen.length === ALL_LAYERS.length ? "" : chosen.join(",") || "-",
      nx: [...state.narrativesOff].join(",") || "-",
      tx: [...state.topicsOff].join(","),
      ax: [...state.actorsOff].join(","),
    });
  }

  function applyURL() {
    const p = new URLSearchParams(window.location.search);
    TT.setSelect($("min-quality"), p.get("q"));
    setStance(["pro", "contra", "neutral"].includes(p.get("stance")) ? p.get("stance") : "all");
    setMode(p.get("mode") === "mix" ? "mix" : "share");
    if (p.get("layers")) {
      const chosen = new Set(p.get("layers").split(","));
      $("layers").querySelectorAll("input").forEach((i) => { i.checked = chosen.has(i.value); });
    }
    const codes = new Set(config.narratives.map((n) => n.code));
    const topics = new Set(config.narratives.map((n) => n.topic));
    state.topicsOff = new Set((p.get("tx") || "").split(",").filter((t) => topics.has(t)));
    state.narrativesOff = p.has("nx")
      ? new Set(p.get("nx").split(",").filter((c) => codes.has(c)))
      : new Set(nested());
    state.actorsOff = new Set((p.get("ax") || "").split(",").filter(Boolean).map(Number).filter(Number.isInteger));
  }

  applyURL();
  renderList();
  load();
})();
