(() => {
  const lang = document.documentElement.lang || "ko";
  const T = window.I18N[lang];
  const D = window.BENCH;
  const V2 = window.BENCH_V2 ?? { models: {}, suites: {} };
  const $ = (s, el = document) => el.querySelector(s);
  const tx = (s, v = {}) => s.replace(/\{(\w+)\}/g, (_, k) => v[k] ?? "");
  const loc = (x) => (x == null ? "" : typeof x === "string" ? x : x[lang] ?? x.ko);
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

  /* ---------- v2 merge (generated data, v2 suites first) ---------- */
  Object.assign(D.models, V2.models);
  for (const role of D.roles) {
    const extra = V2.suites?.[role.id];
    if (extra?.length) role.suites = [...extra, ...role.suites];
  }

  /* ---------- state (URL hash) ---------- */
  const state = { role: D.roles[0].id, suite: null, scope: null, view: "board", sort: null, dir: null, sel: null };
  const readHash = () => {
    const p = new URLSearchParams(location.hash.slice(1));
    for (const k of ["role", "suite", "scope", "view", "sort", "dir", "sel"]) state[k] = p.get(k) ?? (k === "role" ? state.role : k === "view" ? "board" : null);
  };
  const writeHash = () => {
    const p = new URLSearchParams();
    for (const [k, v] of Object.entries(state)) if (v) p.set(k, v);
    history.replaceState(null, "", "#" + p);
  };
  const set = (patch) => { Object.assign(state, patch); writeHash(); render(); };

  const ctx = () => {
    const role = D.roles.find((r) => r.id === state.role) ?? D.roles[0];
    const suite = role.suites.find((s) => s.id === state.suite) ?? role.suites[0];
    const scopeIds = Object.keys(suite.scopes);
    const scopeId = scopeIds.includes(state.scope) ? state.scope : scopeIds[0];
    return { role, suite, scopeId, scope: suite.scopes[scopeId] };
  };

  /* ---------- values & formatting ---------- */
  const metricLabel = (m) => T.metric[m.key] ?? m.key;
  const num = (m, v) => (v == null ? null : m.kind === "ratio" ? v[0] / v[1] : v);
  const sig = (v) => (v >= 100 ? v.toFixed(0) : v >= 10 ? v.toFixed(1) : v >= 0.1 ? v.toFixed(2) : String(+v.toPrecision(2)));
  const fmt = (m, v) => {
    if (v == null) return `<span class="num na">—</span>`;
    switch (m.kind) {
      case "ratio": return `${v[0]}<small>/${v[1]}</small>`;
      case "pct": return `${v.toFixed(1)}<small>%</small>`;
      case "usd": return `$${sig(v)}`;
      case "dur": { const mins = v / 60; return `${mins < 10 ? mins.toFixed(1) : Math.round(mins)}<small>${T.units.min}</small>`; }
      case "tokens": return v >= 1e6 ? `${(v / 1e6).toFixed(1)}<small>M</small>` : `${Math.round(v / 1e3)}<small>K</small>`;
      default: return `${v}`;
    }
  };
  const plain = (m, v) => fmt(m, v).replace(/<[^>]+>/g, "");
  const cmp = (m, a, b) => {
    const x = num(m, a), y = num(m, b);
    if (x == null && y == null) return 0;
    if (x == null) return 1;
    if (y == null) return -1;
    return m.better === "low" ? x - y : y - x;
  };
  const model = (row) => D.models[row.model];
  const vendor = (row) => D.vendors[model(row)?.vendor] ?? { label: model(row)?.vendor ?? "", color: "#8e897d" };
  const color = (row) => vendor(row).color;
  const harness = (row) => `<span class="hn">harness <b>${esc(row.harness ?? D.harness)}</b></span>`;
  const reps = (row) => (row.n > 1 ? ` · ${tx(T.repeats, { n: row.n })}` : "");
  // Secondary per-task values (cost, time …) shown under a matrix cell or beside a drawer verdict.
  const subs = (scope, tv) => scope.metrics.filter((m) => !m.primary && m.key !== scope.taskKey && tv?.[m.key] != null).map((m) => plain(m, tv[m.key]));
  const rowId = (row) => row.id ?? `${row.model}:${row.effort ?? ""}`;
  const effBadge = (row) => (row.effort ? `<span class="eff">${esc(row.effort)}</span>` : "");
  const contaminated = (row) => !!row.flags?.contaminated;
  // A row missing any cohort pack has no comparable score; draw it unranked
  // below every ranked row, exactly like a contaminated row.
  const incomplete = (row) => !!row.flags?.incomplete;
  const unranked = (row) => contaminated(row) || incomplete(row);
  // Row flags (v2 only). 차이 작음 is meaningful only against the default ranking order.
  const flagTags = (row) => (row.flags ? [
    row.flags.contaminated ? `<span class="flag">${T.flagContaminated}</span>` : "",
    row.flags.critical ? `<span class="flag">${T.flagCritical}</span>` : "",
    row.flags.incomplete ? `<span class="flag soft">${T.flagIncomplete}</span>` : "",
    row.flags.close && !state.sort ? `<span class="flag soft">${T.flagClose}</span>` : "",
  ].join("") : "");

  const ranked = (scope) => {
    const ms = scope.rank.map((k) => scope.metrics.find((m) => m.key === k)).filter(Boolean);
    return [...scope.rows].sort((a, b) => {
      if (unranked(a) !== unranked(b)) return unranked(a) ? 1 : -1;
      for (const m of ms) { const c = cmp(m, a.values[m.key], b.values[m.key]); if (c) return c; }
      return 0;
    });
  };

  /* ---------- toolbar ---------- */
  const renderBar = ({ role, suite, scopeId, scope }) => {
    const hasTasks = suite.tasks.length > 0 && scope.rows.some((r) => r.tasks);
    const hasTime = scope.metrics.some((m) => m.key === "time") && scope.scatter?.x !== "time";
    const views = [["board", T.viewBoard], ...(scope.scatter ? [["scatter", T.viewScatter], ...(hasTime ? [["scatterTime", T.viewTime]] : [])] : []), ...(hasTasks ? [["matrix", T.viewMatrix]] : [])];
    if (!views.some(([v]) => v === state.view)) state.view = "board";
    const seg = (items, cur, key) => `<div class="seg" role="group">${items.map(([id, label]) => `<button data-${key}="${id}" aria-pressed="${id === cur}">${label}</button>`).join("")}</div>`;
    // v2 and v1 can share a suite label; show the protocol only when the labels collide.
    // Scout cohorts differ only by judge, so name the judge in the toggle.
    const suiteLabel = (s) => {
      const clash = role.suites.filter((x) => loc(x.label) === loc(s.label)).length > 1;
      const judge = s.judge ? D.models[s.judge]?.short : "";
      return [loc(s.label), clash && s.protocol ? s.protocol : "",
        judge ? `${T.judge} ${judge}` : ""].filter(Boolean).join(" · ");
    };
    const right = [
      suite.archived ? `<span class="tag">${T.archived}</span>` : "",
      role.suites.length > 1 ? seg(role.suites.map((s) => [s.id, suiteLabel(s)]), suite.id, "suite") : "",
      Object.keys(suite.scopes).length > 1 ? seg(Object.keys(suite.scopes).map((id) => [id, id === "worker" ? T.scopeWorker : T.scopeChain]), scopeId, "scope") : "",
    ].join("");
    $("#bar").innerHTML = `
      <div class="views" role="tablist">${views.map(([id, label]) => `<button class="view" role="tab" data-view="${id}" aria-selected="${id === state.view}">${label}</button>`).join("")}</div>
      <div class="bar-right">${right}</div>`;
  };

  /* ---------- board ---------- */
  const modelCell = (row) => {
    const m = model(row);
    return `<div class="m"><span class="sw" style="background:${color(row)}"></span><div>
      <div class="m-name">${esc(m.name)}${effBadge(row)}${flagTags(row)}</div>
      <div class="m-meta"><span>${esc(vendor(row).label)}${reps(row)}</span>${harness(row)}</div>
    </div></div>`;
  };
  const barCell = (m, row, max) => {
    const v = row.values[m.key];
    const x = num(m, v);
    const fill = m.bar && x != null ? `<div class="fill${m.primary ? "" : " neutral"}" style="width:${(x / max) * 100}%;${m.primary ? `background:${color(row)}` : ""}"></div>` : "";
    const ci = m.kind === "pct" && row.ci?.[m.key] ? `<div class="ci" style="left:${(row.ci[m.key][0] / max) * 100}%;width:${((row.ci[m.key][1] - row.ci[m.key][0]) / max) * 100}%"></div>` : "";
    return `<div class="cell">${m.bar ? `<div class="track">${fill}${ci}</div>` : ""}<span class="num${m.bar ? "" : " sub"}">${fmt(m, v)}</span></div>`;
  };
  const maxOf = (m, rows) => (m.kind === "ratio" ? 1 : m.kind === "pct" ? 100 : Math.max(...rows.map((r) => num(m, r.values[m.key]) ?? 0)) || 1);

  const renderBoard = (c) => {
    const { scope } = c;
    const official = ranked(scope);
    let rows = official;
    const sm = scope.metrics.find((m) => m.key === state.sort);
    if (sm) {
      rows = [...official].sort((a, b) => cmp(sm, a.values[sm.key], b.values[sm.key]) * (state.dir === "rev" ? -1 : 1));
    }
    const head = scope.metrics.map((m) => {
      const on = sm === m;
      const arrow = on ? (state.dir === "rev" ? "↑" : "↓") : "";
      return `<th class="sortable${m.bar ? "" : " hide-sm"}" data-sort="${m.key}"${on ? ` aria-sort="${state.dir === "rev" ? "ascending" : "descending"}"` : ""}>${metricLabel(m)}${m.perTask ? ` · ${T.perTask}` : ""}<span class="dir">${arrow}</span></th>`;
    }).join("");
    const body = rows.map((row) => `
      <tr data-row="${rowId(row)}" tabindex="0" aria-selected="${state.sel === rowId(row)}">
        <td class="rk">${unranked(row) ? "" : official.indexOf(row) + 1}</td>
        <td>${modelCell(row)}</td>
        ${scope.metrics.map((m) => `<td class="${m.bar ? "" : "hide-sm"}">${barCell(m, row, maxOf(m, scope.rows))}</td>`).join("")}
      </tr>`).join("");
    return `<div class="scroll"><table class="board"><thead><tr><th class="rk">${T.colRank}</th><th>${T.colModel}</th>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
  };

  /* ---------- scatter ---------- */
  const USD_TICKS = [0.001, 0.003, 0.01, 0.03, 0.1, 0.3, 1, 3, 10, 30, 100, 300, 1000, 3000];
  const TIME_TICKS = [15, 30, 45, 60, 90, 120, 180, 300, 450, 600, 900, 1200, 1800, 2700, 3600, 5400, 7200];
  const axisHint = (m) => (m.kind === "usd" ? ` ${T.axis.cost}` : m.kind === "dur" ? ` ${T.axis.time}` : "");
  const renderScatter = ({ scope }, xKey = scope.scatter.x) => {
    const mx = scope.metrics.find((m) => m.key === xKey);
    const my = scope.metrics.find((m) => m.key === scope.scatter.y);
    const pts = scope.rows.filter((r) => num(mx, r.values[mx.key]) != null && num(my, r.values[my.key]) != null);
    const W = 1056, H = 420, L = 56, R = 170, Tp = 28, B = 48, iw = W - L - R, ih = H - Tp - B;
    const xs = pts.map((r) => num(mx, r.values[mx.key]));
    // Log axis: zero/near-zero values pin to the smallest positive value so
    // Math.log never sees a nonpositive number (all-zero data uses 1).
    const positive = xs.filter((v) => v > 0);
    const minPos = positive.length ? Math.min(...positive) : 1;
    const lo = minPos / 1.6, hi = Math.max(...xs, minPos) * 1.6;
    const X = (v) => L + (1 - (Math.log(Math.max(v, minPos)) - Math.log(lo)) / (Math.log(hi) - Math.log(lo))) * iw;
    const top = my.kind === "pct" ? 100 : 1;
    const ys = pts.map((r) => num(my, r.values[my.key]));
    const ylo = Math.max(0, Math.floor((Math.min(...ys) - top * 0.15) * (10 / top)) / (10 / top));
    const Y = (v) => Tp + (1 - (v - ylo) / (top - ylo)) * ih;
    const yt = Array.from({ length: 5 }, (_, i) => ylo + ((top - ylo) * i) / 4);
    const xt = (mx.kind === "dur" ? TIME_TICKS : USD_TICKS).filter((v) => v >= lo && v <= hi);
    const ytxt = (v) => (my.kind === "pct" ? `${v.toFixed(0)}%` : `${Math.round(v * 100)}%`);
    const placed = [];
    const dots = [...pts].sort((a, b) => num(my, b.values[my.key]) - num(my, a.values[my.key])).map((r) => {
      const x = X(num(mx, r.values[mx.key])), y = Y(num(my, r.values[my.key]));
      let ly = y + 4;
      while (placed.some((p) => Math.abs(p.x - x) < 170 && Math.abs(p.y - ly) < 17)) ly += 17;
      placed.push({ x, y: ly });
      const name = model(r).short;
      return `<g class="pt" data-row="${rowId(r)}" tabindex="0">
        <circle cx="${x}" cy="${y}" r="7" fill="${color(r)}" stroke="#fff" stroke-width="2"/>
        ${ly !== y + 4 ? `<line x1="${x + 6}" y1="${y}" x2="${x + 11}" y2="${ly - 4}" stroke="#d6d1c6"/>` : ""}
        <text x="${x + 13}" y="${ly}">${esc(name)}<tspan class="e" dx="5">${esc(r.effort ?? "")}</tspan></text>
      </g>`;
    }).join("");
    return `<div class="chart"><svg viewBox="0 0 ${W} ${H}" role="img">
      ${yt.map((v) => `<line class="grid" x1="${L}" x2="${W - R}" y1="${Y(v)}" y2="${Y(v)}"/><text class="tick" x="${L - 10}" y="${Y(v) + 4}" text-anchor="end">${ytxt(v)}</text>`).join("")}
      ${xt.map((v) => `<text class="tick" x="${X(v)}" y="${H - B + 20}" text-anchor="middle">${plain(mx, v)}</text>`).join("")}
      <line class="axis" x1="${L}" x2="${W - R}" y1="${H - B}" y2="${H - B}"/>
      <text class="alabel" x="${L}" y="${Tp - 12}">${metricLabel(my)}</text>
      <text class="alabel" x="${(L + W - R) / 2}" y="${H - 6}" text-anchor="middle">${metricLabel(mx)}${axisHint(mx)}</text>
      <text class="good" x="${W - R}" y="${Tp - 12}" text-anchor="end">${T.good}</text>
      ${dots}
    </svg></div>`;
  };

  /* ---------- matrix ---------- */
  const cellStyle = (v) => {
    if (v == null) return "background:var(--soft);color:#c7c2b6";
    if (typeof v === "number") return `background:rgba(11,143,85,${0.03 + 0.17 * (v / 100) ** 2});color:${v >= 80 ? "var(--pass)" : v < 50 ? "var(--fail)" : "var(--text)"}`;
    const r = Array.isArray(v) ? v[0] / v[1] : v ? 1 : 0;
    return r === 1 ? "background:rgba(11,143,85,.11);color:var(--pass)" : `background:rgba(185,28,28,${0.05 + 0.12 * (1 - r)});color:var(--fail)`;
  };
  const renderMatrix = ({ suite, scope }) => {
    const rows = ranked(scope);
    return `<div class="scroll"><table class="matrix"><thead><tr><th></th>${suite.tasks.map((t) => `<th>${esc(loc(t.label))}</th>`).join("")}</tr></thead><tbody>
      ${rows.map((r) => `<tr data-row="${rowId(r)}"><td class="mname">${modelCell(r)}</td>${suite.tasks.map((t) => {
        const tv = r.tasks?.[t.id];
        const v = tv?.[scope.taskKey];
        const label = v == null ? "—" : typeof v === "number" ? `${v}%` : Array.isArray(v) ? `${v[0]}/${v[1]}` : v ? T.pass : T.fail;
        const sub = subs(scope, tv);
        return `<td class="mc" style="${cellStyle(v)}">${label}${sub.length ? `<small>${sub.join(" · ")}</small>` : ""}</td>`;
      }).join("")}</tr>`).join("")}
    </tbody></table></div>`;
  };

  /* ---------- field band ---------- */
  const renderField = ({ role }) => {
    const f = role.field;
    if (!f || state.view !== "board") return "";
    const rows = f.rows.map((r) => `<tr><td>${modelCell(r)}</td>${f.metrics.map((m) => `<td><span class="num sub">${fmt(m, r.values[m.key])}</span></td>`).join("")}</tr>`).join("");
    return `<div class="field"><h3>${T.field}<span class="tag">${T.fieldTag}</span><span class="tag">${esc(loc(f.period))}</span></h3>
      <div class="scroll"><table class="board"><thead><tr><th>${T.fieldModel}</th>${f.metrics.map((m) => `<th>${metricLabel(m)}</th>`).join("")}</tr></thead><tbody>${rows}</tbody></table></div></div>`;
  };

  /* ---------- drawer ---------- */
  // v2 rows carry a compact run detail block; v1 rows have none.
  const detailBlock = (row) => {
    const d = row.detail;
    if (!d) return "";
    const pct = (x) => `${Math.round(x * 100)}%`;
    const lines = [
      d.modelShare != null && d.toolShare != null ? `<div>${T.detailModel} ${pct(d.modelShare)} · ${T.detailTool} ${pct(d.toolShare)}</div>` : "",
      d.concurrency != null ? `<div>${T.detailConcurrency} ${d.concurrency}</div>` : "",
      d.web > 0 ? `<div>${T.detailWeb} ${tx(T.times, { n: d.web })}</div>` : "",
      d.blocked > 0 ? `<div>${T.detailBlocked} ${tx(T.times, { n: d.blocked })}</div>` : "",
      d.access?.length ? `<div>${T.detailAccess}: ${d.access.map((a) => `${esc(T.detailCat[a.category] ?? a.category)} ${tx(T.times, { n: a.count })}`).join(" · ")}</div>` : "",
    ].filter(Boolean);
    return lines.length ? `<div class="d-detail">${lines.join("")}</div>` : "";
  };
  const renderDrawer = (c) => {
    const { suite, scope } = c;
    const row = scope.rows.find((r) => rowId(r) === state.sel);
    $("#scrim").classList.toggle("open", !!row);
    $("#drawer").classList.toggle("open", !!row);
    if (!row) return;
    const rank = ranked(scope).indexOf(row) + 1;
    const kicker = [unranked(row) ? "" : `${rank}위`, `${esc(vendor(row).label)}${reps(row)}`, harness(row)].filter(Boolean).join(" · ");
    const tasks = row.tasks ? suite.tasks.map((t) => {
      const tv = row.tasks[t.id] ?? {};
      const v = tv[scope.taskKey];
      const ok = v == null || typeof v === "number" ? null : Array.isArray(v) ? v[0] === v[1] : !!v;
      const ticks = Array.isArray(v) ? `<div class="ticks">${Array.from({ length: v[1] }, (_, i) => `<i class="${i < v[0] ? "" : "x"}"></i>`).join("")}</div>` : "";
      return `<div class="d-task"><div class="d-task-h"><b>${esc(loc(t.label))}</b>
        <span>${typeof v === "number" ? `<span class="d-verdict">${v}%</span> · ` : ok == null ? "" : `<span class="d-verdict ${ok ? "ok" : "no"}">${Array.isArray(v) ? `${v[0]}/${v[1]}` : ok ? T.pass : T.fail}</span> · `}${subs(scope, tv).join(" · ")}</span></div>${ticks}</div>`;
    }).join("") : "";
    $("#drawer-body").innerHTML = `
      <div class="d-kicker">${kicker}</div>
      <h2 class="d-name"><span class="sw" style="background:${color(row)};margin:0"></span>${esc(model(row).name)}${effBadge(row)}</h2>
      <div class="d-stats">${scope.metrics.slice(0, 3).map((m) => `<div><div class="stat-v">${fmt(m, row.values[m.key])}</div><div class="stat-k">${metricLabel(m)}${m.perTask ? ` · ${T.perTask}` : ""}</div></div>`).join("")}</div>
      ${detailBlock(row)}
      ${tasks ? `<div class="stat-k" style="margin-bottom:4px">${T.drawerTasks}</div>${tasks}` : ""}`;
  };

  /* ---------- page ---------- */
  const moveInk = () => {
    const on = $(".role[aria-selected=true]");
    const ink = $(".role-ink");
    if (!on || !ink) return;
    ink.style.width = on.offsetWidth + "px";
    ink.style.transform = `translateX(${on.offsetLeft}px)`;
  };

  const render = () => {
    const c = ctx();
    $("#roles").innerHTML = D.roles.map((r) => `<button class="role" role="tab" data-role="${r.id}" aria-selected="${r.id === c.role.id}">${T.role[r.id]}</button>`).join("") + `<span class="role-ink"></span>`;
    moveInk();
    renderBar(c);
    const view = state.view === "scatter" ? renderScatter(c) : state.view === "scatterTime" ? renderScatter(c, "time") : state.view === "matrix" ? renderMatrix(c) : renderBoard(c);
    $("#view").innerHTML = view + renderField(c);
    $("#foot-meta").textContent = tx(T.footRuns, { runs: c.suite.runs.toLocaleString(lang), date: c.suite.date, protocol: c.suite.protocol });
    renderDrawer(c);
  };

  /* ---------- events ---------- */
  document.addEventListener("click", (e) => {
    const t = e.target.closest("[data-role],[data-view],[data-scope],[data-suite],[data-sort],[data-row],#scrim,.d-close");
    if (!t) return;
    if (t.dataset.role) return set({ role: t.dataset.role, suite: null, scope: null, view: "board", sort: null, dir: null, sel: null });
    if (t.dataset.view) return set({ view: t.dataset.view });
    if (t.dataset.scope) return set({ scope: t.dataset.scope, sort: null, dir: null, sel: null });
    if (t.dataset.suite) return set({ suite: t.dataset.suite, scope: null, sort: null, dir: null, sel: null });
    if (t.dataset.sort) {
      if (state.sort !== t.dataset.sort) return set({ sort: t.dataset.sort, dir: null });
      return state.dir ? set({ sort: null, dir: null }) : set({ dir: "rev" });
    }
    if (t.dataset.row) return set({ sel: t.dataset.row });
    set({ sel: null });
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && state.sel) set({ sel: null });
    if (e.key === "Enter" && e.target.dataset?.row) set({ sel: e.target.dataset.row });
  });
  document.addEventListener("pointerover", (e) => {
    const pt = e.target.closest(".pt");
    const chart = $(".chart");
    if (!chart) return;
    chart.classList.toggle("dim", !!pt);
    chart.querySelectorAll(".pt.on").forEach((g) => g.classList.remove("on"));
    if (pt) pt.classList.add("on");
  });
  window.addEventListener("resize", moveInk);
  window.addEventListener("hashchange", () => { readHash(); render(); });

  readHash();
  render();
  document.fonts?.ready.then(moveInk);
})();
