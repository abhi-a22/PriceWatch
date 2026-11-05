// PriceWatch dashboard. Vanilla JS, no dependencies.
// Every piece of dynamic content is built with createElement/textContent, never by
// assigning HTML strings, so server data can never be interpreted as markup.
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const SVG_NS = "http://www.w3.org/2000/svg";
  const WINDOW_LABELS = { 10: "10s", 30: "30s", 60: "1m", 120: "2m", 300: "5m", 600: "10m" };

  // ------------------------------------------------------------------ helpers
  function el(tag, opts = {}, children = []) {
    const node = document.createElement(tag);
    if (opts.text !== undefined) node.textContent = opts.text;
    if (opts.cls) node.className = opts.cls;
    if (opts.attrs) for (const [k, v] of Object.entries(opts.attrs)) node.setAttribute(k, v);
    if (opts.on) for (const [k, fn] of Object.entries(opts.on)) node.addEventListener(k, fn);
    for (const child of children) node.append(child);
    return node;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function fmtPrice(p) {
    const digits = p < 10 ? 4 : 2;
    return p.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: digits });
  }

  function fmtNum(n) {
    return n.toLocaleString(undefined, { maximumFractionDigits: 4 });
  }

  function store(key, value) {
    try {
      if (value === null) sessionStorage.removeItem(key);
      else sessionStorage.setItem(key, value);
    } catch (_) { /* storage can be unavailable (private mode); the app still works */ }
  }

  function load(key) {
    try { return sessionStorage.getItem(key); } catch (_) { return null; }
  }

  let token = load("pw_token");
  let email = load("pw_email");
  let authMode = "login";

  async function api(path, { method = "GET", body, auth = true } = {}) {
    const headers = {};
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (auth && token) headers.Authorization = "Bearer " + token;
    const res = await fetch(path, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    let data = null;
    if (res.status !== 204) {
      try { data = await res.json(); } catch (_) { /* non-JSON error body */ }
    }
    if (!res.ok) {
      if (res.status === 401 && auth && token) signOut();
      throw new Error((data && data.error) || "Request failed (" + res.status + ")");
    }
    return data;
  }

  function showError(node, message) {
    node.textContent = message || "";
    node.hidden = !message;
  }

  // ------------------------------------------------------------------- prices
  const rows = new Map();
  let symbolsKey = "";

  function sparkline(values, up) {
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 110 26");
    svg.setAttribute("class", "spark " + up);
    svg.setAttribute("aria-hidden", "true");
    if (values.length >= 2) {
      const min = Math.min(...values);
      const range = Math.max(...values) - min || 1;
      const points = values.map((v, i) => {
        const x = (i / (values.length - 1)) * 110;
        const y = 24 - ((v - min) / range) * 22;
        return x.toFixed(1) + "," + y.toFixed(1);
      });
      const line = document.createElementNS(SVG_NS, "polyline");
      line.setAttribute("points", points.join(" "));
      svg.append(line);
    }
    return svg;
  }

  function flash(node, direction) {
    node.classList.remove("flash-up", "flash-down");
    void node.offsetWidth; // restart the CSS animation
    node.classList.add(direction > 0 ? "flash-up" : "flash-down");
  }

  function renderPrices(list) {
    const body = $("prices-body");
    if (rows.size === 0 && list.length) clear(body);
    for (const item of list) {
      let row = rows.get(item.symbol);
      if (!row) {
        row = {
          price: el("td", { cls: "num" }),
          change: el("td", { cls: "num" }),
          spark: el("td"),
          last: null,
          sparkKey: "",
        };
        row.tr = el("tr", {}, [el("td", { cls: "sym", text: item.symbol }), row.price, row.change, row.spark]);
        rows.set(item.symbol, row);
        body.append(row.tr);
      }
      row.price.textContent = fmtPrice(item.price);
      if (row.last !== null && item.price !== row.last) flash(row.price, item.price - row.last);
      row.last = item.price;

      const ch = item.change_pct_1m;
      if (ch === null || ch === undefined) {
        row.change.textContent = "—";
        row.change.className = "num muted";
      } else {
        row.change.textContent = (ch >= 0 ? "+" : "") + ch.toFixed(2) + "%";
        row.change.className = "num " + (ch > 0 ? "up" : ch < 0 ? "down" : "");
      }

      const spark = item.spark || [];
      const key = spark.join(",");
      if (key !== row.sparkKey) {
        row.sparkKey = key;
        const dir = spark.length < 2 ? "flat" : spark[spark.length - 1] >= spark[0] ? "up" : "down";
        clear(row.spark);
        row.spark.append(sparkline(spark, dir));
      }
    }
    const key = list.map((p) => p.symbol).join(",");
    if (key !== symbolsKey && list.length) {
      symbolsKey = key;
      const select = $("f-symbol");
      const current = select.value;
      clear(select);
      for (const p of list) select.append(el("option", { text: p.symbol, attrs: { value: p.symbol } }));
      if (current) select.value = current;
    }
  }

  async function pollPrices() {
    const data = await api("/api/prices?spark=60", { auth: false });
    renderPrices(data.prices);
  }

  async function pollHealth() {
    const badge = $("status");
    try {
      const h = await api("/api/health", { auth: false });
      const text =
        h.feed.provider + " · " + h.bus.processed.toLocaleString() + " ticks · p95 " +
        h.bus.latency_ms.p95.toFixed(2) + " ms";
      badge.textContent = h.status === "ok" ? text : "feed degraded · " + text;
      badge.className = "badge " + (h.status === "ok" ? "badge-ok" : "badge-warn");
    } catch (_) {
      badge.textContent = "offline";
      badge.className = "badge badge-bad";
    }
  }

  // ------------------------------------------------------------------- alerts
  let alertsKey = "";

  function describeAlert(a) {
    if (a.rule_type === "above") return a.symbol + " at or above " + fmtNum(a.threshold);
    if (a.rule_type === "below") return a.symbol + " at or below " + fmtNum(a.threshold);
    const dir = a.rule_type === "pct_rise" ? "rises" : "falls";
    const win = WINDOW_LABELS[a.window_seconds] || a.window_seconds + "s";
    return a.symbol + " " + dir + " " + fmtNum(a.threshold) + "% within " + win;
  }

  function alertDetails(a) {
    const parts = [a.repeating ? "Repeating, cooldown " + a.cooldown_seconds + "s" : "One-time"];
    if (a.webhook_url) parts.push("webhook " + a.webhook_url);
    if (a.last_triggered_at) parts.push("last fired " + new Date(a.last_triggered_at).toLocaleTimeString());
    return parts.join(" · ");
  }

  async function changeAlert(id, patch) {
    try {
      await api("/api/alerts/" + id, { method: "PATCH", body: patch });
    } catch (err) {
      showError($("alert-error"), err.message);
    }
    await pollAlerts(true);
  }

  async function deleteAlert(id) {
    try {
      await api("/api/alerts/" + id, { method: "DELETE" });
    } catch (err) {
      showError($("alert-error"), err.message);
    }
    await pollAlerts(true);
    await pollEvents();
  }

  function renderAlerts(alerts) {
    const list = $("alerts-list");
    clear(list);
    $("alerts-empty").hidden = alerts.length > 0;
    for (const a of alerts) {
      const toggle = a.active
        ? el("button", {
            text: "Pause",
            cls: "btn btn-ghost btn-small",
            attrs: { type: "button" },
            on: { click: () => changeAlert(a.id, { active: false }) },
          })
        : el("button", {
            text: "Re-arm",
            cls: "btn btn-small",
            attrs: { type: "button" },
            on: { click: () => changeAlert(a.id, { active: true }) },
          });
      const del = el("button", {
        text: "Delete",
        cls: "btn btn-danger btn-small",
        attrs: { type: "button" },
        on: { click: () => deleteAlert(a.id) },
      });
      list.append(
        el("li", { cls: a.active ? "" : "paused" }, [
          el("div", { cls: "grow" }, [
            el("div", { cls: "title", text: describeAlert(a) }),
            el("div", { cls: "sub", text: alertDetails(a) }),
          ]),
          el("span", { cls: "badge " + (a.active ? "badge-ok" : "badge-muted"), text: a.active ? "Active" : "Inactive" }),
          el("div", { cls: "actions" }, [toggle, del]),
        ]),
      );
    }
  }

  async function pollAlerts(force = false) {
    if (!token) return;
    const data = await api("/api/alerts");
    const key = JSON.stringify(data.alerts);
    if (force || key !== alertsKey) {
      alertsKey = key;
      renderAlerts(data.alerts);
    }
  }

  // ------------------------------------------------------------------- events
  const events = new Map();
  let eventsLoaded = false;
  let hasMore = false;
  let eventsKey = "";

  const DELIVERY = {
    pending: ["Webhook pending", "badge-warn"],
    delivered: ["Webhook delivered", "badge-ok"],
    failed: ["Webhook failed", "badge-bad"],
  };

  async function retryEvent(id) {
    try {
      await api("/api/events/" + id + "/retry", { method: "POST" });
    } catch (err) {
      showError($("alert-error"), err.message);
    }
    await pollEvents();
  }

  function renderEvents(freshIds) {
    const list = $("events-list");
    clear(list);
    const sorted = [...events.values()].sort((a, b) => b.id - a.id);
    $("events-empty").hidden = sorted.length > 0;
    $("events-more").hidden = !hasMore;
    for (const e of sorted) {
      const when = new Date(e.fired_at).toLocaleTimeString();
      const sub = e.delivery_status === "failed" && e.delivery_error
        ? e.message + " (" + e.delivery_error + ", " + e.delivery_attempts + " attempts)"
        : e.message;
      const children = [
        el("div", { cls: "grow" }, [
          el("div", { cls: "title", text: e.symbol + " at " + fmtPrice(e.price) + " · " + when }),
          el("div", { cls: "sub", text: sub }),
        ]),
      ];
      const delivery = DELIVERY[e.delivery_status];
      if (delivery) children.push(el("span", { cls: "badge " + delivery[1], text: delivery[0] }));
      if (e.delivery_status === "failed") {
        children.push(
          el("button", {
            text: "Retry",
            cls: "btn btn-small",
            attrs: { type: "button" },
            on: { click: () => retryEvent(e.id) },
          }),
        );
      }
      list.append(el("li", { cls: freshIds.has(e.id) ? "flash-up" : "" }, children));
    }
  }

  function mergeEvents(page, limit) {
    const fresh = new Set();
    const ids = new Set(page.map((e) => e.id));
    // Drop cached events inside the page's id range that the server no longer has
    // (their alert was deleted); a short page means it covered everything.
    const floor = page.length >= limit ? Math.min(...ids) : 0;
    for (const id of [...events.keys()]) if (id >= floor && !ids.has(id)) events.delete(id);
    for (const e of page) {
      if (eventsLoaded && !events.has(e.id)) fresh.add(e.id);
      events.set(e.id, e);
    }
    return fresh;
  }

  async function pollEvents() {
    if (!token) return;
    const limit = 30;
    const data = await api("/api/events?limit=" + limit);
    if (!eventsLoaded) hasMore = data.next_before_id !== null;
    const fresh = mergeEvents(data.events, limit);
    eventsLoaded = true;
    const key = JSON.stringify([...events.values()]);
    if (key !== eventsKey) {
      eventsKey = key;
      renderEvents(fresh);
    }
  }

  async function loadOlder() {
    const oldest = Math.min(...events.keys());
    const data = await api("/api/events?limit=30&before_id=" + oldest);
    hasMore = data.next_before_id !== null;
    for (const e of data.events) events.set(e.id, e);
    eventsKey = "";
    renderEvents(new Set());
  }

  // ------------------------------------------------------------------ auth UI
  function applySession() {
    const signedIn = Boolean(token);
    $("auth-card").hidden = signedIn;
    for (const id of ["alert-card", "alerts-card", "events-card", "logout", "whoami"]) $(id).hidden = !signedIn;
    $("whoami").textContent = signedIn ? email || "" : "";
  }

  function signOut() {
    token = null;
    email = null;
    store("pw_token", null);
    store("pw_email", null);
    events.clear();
    eventsLoaded = false;
    alertsKey = "";
    eventsKey = "";
    applySession();
  }

  function setAuthMode(mode) {
    authMode = mode;
    const login = mode === "login";
    $("tab-login").classList.toggle("active", login);
    $("tab-register").classList.toggle("active", !login);
    $("tab-login").setAttribute("aria-selected", String(login));
    $("tab-register").setAttribute("aria-selected", String(!login));
    $("auth-submit").textContent = login ? "Sign in" : "Create account";
    $("auth-password").setAttribute("autocomplete", login ? "current-password" : "new-password");
    showError($("auth-error"), "");
  }

  async function submitAuth(ev) {
    ev.preventDefault();
    const button = $("auth-submit");
    showError($("auth-error"), "");
    button.disabled = true;
    try {
      const path = authMode === "login" ? "/api/auth/login" : "/api/auth/register";
      const data = await api(path, {
        method: "POST",
        auth: false,
        body: { email: $("auth-email").value, password: $("auth-password").value },
      });
      token = data.token;
      email = data.user.email;
      store("pw_token", token);
      store("pw_email", email);
      $("auth-password").value = "";
      applySession();
      await Promise.all([pollAlerts(true), pollEvents()]);
    } catch (err) {
      showError($("auth-error"), err.message);
    } finally {
      button.disabled = false;
    }
  }

  // ----------------------------------------------------------------- alert form
  function syncAlertForm() {
    const rule = $("f-rule").value;
    const pct = rule === "pct_rise" || rule === "pct_drop";
    $("f-window-wrap").hidden = !pct;
    $("f-threshold-label").textContent = pct ? "Percent change" : "Target price";
    $("f-cooldown-wrap").hidden = !$("f-repeating").checked;
  }

  async function submitAlert(ev) {
    ev.preventDefault();
    showError($("alert-error"), "");
    const rule = $("f-rule").value;
    const threshold = Number($("f-threshold").value);
    if ($("f-threshold").value.trim() === "" || !Number.isFinite(threshold)) {
      showError($("alert-error"), "Enter a number for the target.");
      return;
    }
    const body = { symbol: $("f-symbol").value, rule_type: rule, threshold };
    if (rule === "pct_rise" || rule === "pct_drop") body.window_seconds = Number($("f-window").value);
    body.repeating = $("f-repeating").checked;
    body.cooldown_seconds = body.repeating ? Math.trunc(Number($("f-cooldown").value) || 0) : 0;
    const url = $("f-webhook").value.trim();
    if (url) body.webhook_url = url;
    try {
      await api("/api/alerts", { method: "POST", body });
      $("f-threshold").value = "";
      await pollAlerts(true);
    } catch (err) {
      showError($("alert-error"), err.message);
    }
  }

  // ------------------------------------------------------------------- wiring
  function every(ms, fn) {
    let running = false;
    const tick = async () => {
      if (running || document.hidden) return;
      running = true;
      try { await fn(); } catch (_) { /* transient network error; next tick retries */ } finally { running = false; }
    };
    tick();
    setInterval(tick, ms);
  }

  $("tab-login").addEventListener("click", () => setAuthMode("login"));
  $("tab-register").addEventListener("click", () => setAuthMode("register"));
  $("auth-form").addEventListener("submit", submitAuth);
  $("logout").addEventListener("click", signOut);
  $("alert-form").addEventListener("submit", submitAlert);
  $("f-rule").addEventListener("change", syncAlertForm);
  $("f-repeating").addEventListener("change", syncAlertForm);
  $("events-more").addEventListener("click", () => loadOlder().catch((e) => showError($("alert-error"), e.message)));

  syncAlertForm();
  applySession();
  every(1000, pollPrices);
  every(2000, pollHealth);
  every(4000, () => pollAlerts());
  every(2000, pollEvents);
})();
