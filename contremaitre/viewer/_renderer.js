// Renders DATA into the page scaffold. DATA is injected by build_viewer.

(function () {
  function fmt(n) { return new Intl.NumberFormat().format(n || 0); }
  function bytes(n) {
    if (n == null) return "";
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
    return (n / (1024 * 1024)).toFixed(1) + " MB";
  }
  function dur(s) {
    if (s == null) return "?";
    s = Math.round(Number(s));
    const m = Math.floor(s / 60);
    const r = s - 60 * m;
    if (m === 0) return r + "s";
    return m + "m " + r + "s";
  }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, c => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;",
    }[c]));
  }
  function asJson(v) {
    try { return JSON.stringify(v, null, 2); } catch (_) { return String(v); }
  }

  const stats = DATA.stats || {};
  const tc = stats.tool_counts || {};

  // ----- tagline + title -----

  const verdict = stats.verdict || "?";
  const terminal = stats.terminal_state || "?";
  const reason = stats.reason || "";
  document.getElementById("rep-tagline").innerHTML =
    `<b>${esc(terminal)}</b> · ${esc(verdict)}${reason ? " · " + esc(reason) : ""}`;

  // ----- header totals strip -----

  const cost = stats.cost_usd != null ? "$" + Number(stats.cost_usd).toFixed(4) : "—";
  const verdictTier = verdict === "READY_FOR_DRAFT_PR" ? "tier-green"
                   : verdict === "PR_NEEDS_HUMAN"    ? "tier-yellow"
                   : verdict === "FAILED_INFRA"        ? "tier-red"
                   : verdict.startsWith("NO_PR")       ? "tier-yellow"
                                                       : "tier-unknown";

  // Post-publish CLI review chip — only when the run actually ran one.
  // Tier matches the agent's verdict key (LOOKS_GOOD → green,
  // NEEDS_ATTENTION → yellow, MUST_FIX → red), NOT the subprocess exit
  // status, so `MUST_FIX` reads red even though the subprocess exited
  // cleanly.
  const cli = DATA.cli_review;
  function cliTier(c) {
    if (!c) return "tier-unknown";
    if (c.status !== "completed") return "tier-red";
    if (c.verdict === "LOOKS_GOOD") return "tier-green";
    if (c.verdict === "NEEDS_ATTENTION") return "tier-yellow";
    if (c.verdict === "MUST_FIX") return "tier-red";
    return "tier-unknown";
  }
  // The leading sim-dot IS the verdict signal (tier-green/yellow/red).
  // We deliberately omit any raw emoji here — the clean colored circle
  // is the house-style indicator everywhere else in the viewer.
  function cliReviewChip(c, sourceLabel) {
    const suffix = sourceLabel ? ` <span class="dim">${esc(sourceLabel)}</span>` : "";
    return `<span class="item"><span class="sim-dot ${cliTier(c)}"></span>${esc((c.tool || "cli").toUpperCase())} review${c.model ? " <code>" + esc(c.model) + "</code>" : ""}${c.duration_s != null ? " · <span class=\"dim\">" + esc(dur(c.duration_s)) + "</span>" : ""}${suffix}</span>`;
  }
  function cliReviewBlock(c, sourceLabel) {
    const heading = sourceLabel
      ? `post-publish review (${esc(c.tool)} · ${esc(sourceLabel)})`
      : `post-publish review (${esc(c.tool)})`;
    return `
    <h2>${heading}</h2>
    <div class="meta-grid">
      <div class="k">status</div><div class="v"><span class="sim-dot ${cliTier(c)}"></span><b>${esc(c.status)}</b></div>
      ${c.model ? `<div class="k">model</div><div class="v"><code>${esc(c.model)}</code></div>` : ""}
      ${c.duration_s != null ? `<div class="k">duration</div><div class="v"><b>${esc(dur(c.duration_s))}</b></div>` : ""}
      ${c.url ? `<div class="k">PR comment posted on</div><div class="v"><a href="${esc(c.url)}">${esc(c.url)}</a></div>` : ""}
      ${c.fail_reason ? `<div class="k">failure reason</div><div class="v" style="color:var(--warning)">${esc(c.fail_reason)}</div>` : ""}
    </div>
    ${c.markdown ? `<div class="turn"><div class="turn-body"><pre>${esc(c.markdown)}</pre></div></div>` : ""}
    `;
  }
  const cliChip = cli ? cliReviewChip(cli, null) : "";
  // Each CLI review round (per-tool) gets its own chip next to the
  // original so the side-by-side verdict comparison is visible without
  // leaving the header.
  const cliExtras = DATA.cli_review_extras || [];
  const cliExtraChips = cliExtras.map(c => cliReviewChip(c, c.source)).join("");

  document.getElementById("stats-header").innerHTML = `
    <span class="item"><span class="sim-dot ${verdictTier}"></span>verdict <b>${esc(verdict)}</b></span>
    <span class="item">cost <b>${esc(cost)}</b></span>
    <span class="item">duration <b>${esc(dur(stats.duration_seconds))}</b></span>
    <span class="item">turns <b>${fmt(stats.turns)}</b></span>
    <span class="item">tokens <b>${fmt(stats.tokens_in)}</b> in / <b>${fmt(stats.tokens_out)}</b> out</span>
    <span class="item"><b>${fmt(stats.n_tool_uses)}</b> tool uses</span>
    <span class="item"><b>${fmt(stats.subagent_count)}</b> sub-agents</span>
    <span class="item"><b>${fmt(stats.files_written_count)}</b> files written</span>
    ${stats.files_changed != null ? `<span class="item">diff <b>${fmt(stats.files_changed)}</b> file${stats.files_changed === 1 ? "" : "s"} · <b>+${fmt(stats.lines_added)}</b> / <b>−${fmt(stats.lines_removed)}</b></span>` : ""}
    ${cliChip}
    ${cliExtraChips}
  `;

  // ----- tab nav -----

  const TABS = [
    ["overview",     "overview"],
    ["conversation", "conversation"],
    ["timeline",     "timeline"],
    ["subagents",    "sub-agents"],
    ["files",        "files written"],
    ["events",       "events"],
    ["eval",         "eval"],
  ];
  const nav = document.getElementById("nav");
  TABS.forEach(([id, label], i) => {
    const b = document.createElement("button");
    b.textContent = label;
    if (i === 0) b.classList.add("active");
    b.onclick = () => {
      document.querySelectorAll(".viewer-nav button").forEach(x => x.classList.remove("active"));
      document.querySelectorAll("section").forEach(x => x.classList.remove("active"));
      b.classList.add("active");
      document.getElementById(id).classList.add("active");
    };
    nav.appendChild(b);
  });

  // ----- overview -----

  const toolPills = Object.entries(tc).sort((a, b) => b[1] - a[1])
    .map(([k, v]) => `<span class="pill ${esc(k)}">${esc(k)}:${v}</span>`).join(" ");

  const pr = DATA.pr;
  const prRow = pr ? `
    <div class="k">publication outcome</div>
    <div class="v">
      <b>${esc(pr.kind || "?")}</b>
      ${pr.url ? ` · <a href="${esc(pr.url)}">${esc(pr.url)}</a>` : ""}
      ${pr.branch ? ` · branch <code>${esc(pr.branch)}</code>` : ""}
      ${pr.dry_run ? ` <span class="pill">dry-run</span>` : ""}
    </div>
  ` : "";

  document.getElementById("overview").innerHTML = `
    <h2>run metadata</h2>
    <div class="meta-grid">
      <div class="k">run id</div><div class="v"><code>${esc(stats.run_id || DATA.run_id || "")}</code></div>
      <div class="k">terminal state</div><div class="v"><b>${esc(stats.terminal_state || "?")}</b></div>
      <div class="k">verdict</div><div class="v"><b>${esc(stats.verdict || "?")}</b></div>
      <div class="k">reason</div><div class="v">${esc(stats.reason || "—")}</div>
      <div class="k">agent model</div><div class="v"><code>${esc(stats.agent_model || "?")}</code></div>
      <div class="k">sim model</div><div class="v"><code>${esc(stats.sim_model || "?")}</code></div>
      <div class="k">actor mode</div><div class="v">${esc(stats.actor_mode || "?")}</div>
      <div class="k">publish mode</div><div class="v">${esc(stats.publish_mode || "?")}</div>
      <div class="k">turns</div><div class="v"><b>${fmt(stats.turns)}</b></div>
      <div class="k">duration</div><div class="v"><b>${esc(dur(stats.duration_seconds))}</b></div>
      <div class="k">events</div><div class="v"><b>${fmt(stats.n_events)}</b> (${fmt(stats.n_text_events)} text · ${fmt(stats.n_tool_uses)} tool_use · ${fmt(stats.n_step_finishes)} step_finish)</div>
      <div class="k">tool counts (agent + sim)</div><div class="v">${toolPills || '<span style="color:var(--text-dim)">none</span>'}</div>
      <div class="k">tokens</div><div class="v"><b>${fmt(stats.tokens_in)}</b> in / <b>${fmt(stats.tokens_out)}</b> out</div>
      ${stats.files_changed != null ? `<div class="k">net diff</div><div class="v"><b>${fmt(stats.files_changed)}</b> file${stats.files_changed === 1 ? "" : "s"} · <span style="color:var(--success)">+${fmt(stats.lines_added)}</span> / <span style="color:#F87171">−${fmt(stats.lines_removed)}</span></div>` : ""}
      <div class="k">recorded cost</div><div class="v">$<b>${(stats.cost_usd ?? 0).toFixed(4)}</b></div>
      ${prRow}
    </div>

    ${cli ? cliReviewBlock(cli, null) : ""}
    ${cliExtras.map(c => cliReviewBlock(c, c.source)).join("")}

    <h2>initial prompt</h2>
    <div class="turn"><div class="turn-body"><pre>${esc(DATA.initial_prompt || "—")}</pre></div></div>

    <h2>full conversation</h2>
    <div id="chat-mount"></div>
  `;

  // ----- conversation -----

  const transcript = DATA.transcript || [];
  const convSec = document.getElementById("conversation");
  if (!transcript.length) {
    convSec.innerHTML = `<div class="empty">No transcript captured.</div>`;
  } else {
    convSec.innerHTML = `<h2>turn-by-turn transcript (${transcript.length} turns)</h2>` +
      transcript.map((t, i) => {
        const speaker = (t.speaker || "").toLowerCase();
        const cls = speaker.startsWith("agent") ? "agent"
                  : speaker.startsWith("sim")   ? "sim"
                  : "user";
        return `
          <div class="turn">
            <div class="turn-head">
              <span class="role ${cls}">${esc(t.speaker || "?")}</span>
              <span>${esc(t.phase || "")}</span>
              <span>turn ${i + 1}</span>
              <span style="margin-left:auto">${fmt((t.text || "").length)} chars</span>
            </div>
            <div class="turn-body"><pre>${esc(t.text || "")}</pre></div>
          </div>`;
      }).join("");
  }

  // ----- timeline -----

  const tl = DATA.timeline || [];
  const tlSec = document.getElementById("timeline");
  if (!tl.length) {
    tlSec.innerHTML = `<div class="empty">No timeline events captured.</div>`;
  } else {
    tlSec.innerHTML = `<h2>event timeline (${tl.length} events)</h2>` +
      tl.map((e, i) => {
        const t = e.type;
        let info = "";
        if (t === "text") {
          info = `<span class="dim">${fmt(e.text_len)} chars</span> ${esc((e.text_preview || "").slice(0, 120))}`;
        } else if (t === "tool_use") {
          const parts = [];
          if (e.status && e.status !== "completed") parts.push(`<span class="dim">status:</span> ${esc(e.status)}`);
          if (e.title) parts.push(esc(e.title));
          if (e.filePath) parts.push(`<span class="dim">file:</span> ${esc(e.filePath)}`);
          if (e.command) parts.push(`<span class="dim">cmd:</span> ${esc(e.command)}`);
          if (e.description) parts.push(`<span class="dim">desc:</span> ${esc(e.description)}`);
          if (e.pattern) parts.push(`<span class="dim">pat:</span> ${esc(e.pattern)}`);
          info = parts.join(" &nbsp; ");
        } else if (t === "step_finish") {
          const tok = e.tokens || {};
          info = `<span class="dim">in</span> ${fmt(tok.input)} <span class="dim">out</span> ${fmt(tok.output)} <span class="dim">cost</span> $${Number(e.cost || 0).toFixed(4)}`;
        }
        const tool = e.tool ? `<span class="tool ${esc(e.tool)}">${esc(e.tool)}</span>` : "";
        const actorTag = e.actor ? `<span class="pill">${esc(e.actor)}</span>` : "";
        return `
          <div class="tl-row ${esc(t)}">
            <div class="idx">${i}</div>
            <div class="type">${esc(t)} ${actorTag}</div>
            <div>${tool}</div>
            <div class="info">${info}</div>
          </div>`;
      }).join("");
  }

  // ----- sub-agents -----

  const sub = DATA.sub_agents || [];
  const subSec = document.getElementById("subagents");
  if (!sub.length) {
    subSec.innerHTML = `<div class="empty">No sub-agents extracted.</div>`;
  } else {
    subSec.innerHTML = `<h2>sub-agent tasks (${sub.length})</h2>` +
      sub.map(a => `
        <details class="collapse">
          <summary>
            <span>${esc(a.name)}</span>
            <span class="meta">${bytes(a.len ?? (a.body || "").length)}</span>
          </summary>
          <div class="body"><pre>${esc(a.body || "")}</pre></div>
        </details>`).join("");
  }

  // ----- files written -----

  const files = DATA.extracted_files || [];
  const filesSec = document.getElementById("files");
  if (!files.length) {
    filesSec.innerHTML = `<div class="empty">No files written by agent.</div>`;
  } else {
    filesSec.innerHTML = `<h2>files written by agent (${files.length})</h2>` +
      files.map(f => `
        <details class="collapse">
          <summary>
            <span>${esc(f.name)}</span>
            <span class="meta">${bytes(f.len ?? (f.body || "").length)}</span>
          </summary>
          <div class="body"><pre>${esc(f.body || "")}</pre></div>
        </details>`).join("");
  }

  // ----- events (guardrails + recoveries) -----

  const events = DATA.guardrails || [];
  const recoveries = DATA.recoveries || [];
  const eventsSec = document.getElementById("events");

  function evRow(e, i, kind) {
    const name = e.event || e.kind || "?";
    const rest = { ...e };
    delete rest.event; delete rest.kind; delete rest.ts;
    const info = Object.keys(rest).length ? asJson(rest).replace(/\s+/g, " ") : "";
    return `
      <div class="ev-row ${kind}">
        <div class="idx">${i}</div>
        <div class="ts">${esc(e.ts || "")}</div>
        <div class="name" title="${esc(name)}">${esc(name)}</div>
        <div class="info" title="${esc(info)}">${esc(info)}</div>
      </div>`;
  }

  if (!events.length && !recoveries.length) {
    eventsSec.innerHTML = `<div class="empty">No guardrail or recovery events.</div>`;
  } else {
    const guardRows = events.map((e, i) => evRow(e, i, "guardrail")).join("");
    const recRows   = recoveries.map((e, i) => evRow(e, i, "recovery")).join("");
    eventsSec.innerHTML =
      (events.length ? `<h2>guardrail events (${events.length})</h2>${guardRows}` : "") +
      (recoveries.length ? `<h2>recoveries (${recoveries.length})</h2>${recRows}` : "");
  }

  // ----- chat (bubble + tool trace per turn) -----

  const chat = DATA.chat || { turns: [], totals: {}, duration: 0 };
  // The chat now lives inline in the overview, mounted under the initial
  // prompt — there is no longer a standalone "chat" tab.
  const chatSec = document.getElementById("chat-mount");

  function clock(s) {
    s = Math.round(s || 0);
    const m = Math.floor(s / 60);
    const r = s - 60 * m;
    return m + ":" + String(r).padStart(2, "0");
  }
  function ktok(n) {
    n = Number(n) || 0;
    return n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n);
  }
  function money(n) {
    n = Number(n) || 0;
    return "$" + n.toFixed(4);
  }
  function statusClass(s) {
    if (s === "error" || s === "failed") return "error";
    if (s === "running" || s === "pending") return "running";
    return "";
  }
  function chips(summary) {
    const ord = ["skill", "task", "read", "glob", "grep", "todowrite", "write", "edit", "bash"];
    return Object.keys(summary || {})
      .sort((a, b) => ord.indexOf(a) - ord.indexOf(b))
      .map(k => `${esc(k)}×${summary[k]}`).join("  ");
  }

  // Per-tool body renderer — picks the right shape for the call.
  function preBox(s) { return `<pre class="box">${esc(s)}</pre>`; }
  function kv(k, v) {
    return `<div class="kv"><span class="k">${esc(k)}</span><span class="v">${esc(typeof v === "object" ? asJson(v) : v)}</span></div>`;
  }
  function toolBody(t) {
    const inp = t.input || {};
    const out = t.output || "";
    let h = "";
    if (t.tool === "bash") {
      if (inp.command) h += `<div class="label">command</div>${preBox("$ " + inp.command)}`;
      if (inp.description) h += `<div class="label">description</div>${preBox(inp.description)}`;
      h += `<div class="label">output</div>${preBox(out || "(no output)")}`;
    } else if (t.tool === "edit") {
      if (inp.filePath) h += `<div class="label">file</div>${preBox(inp.filePath)}`;
      if (inp.oldString != null) h += `<div class="label">old</div>${preBox(inp.oldString)}`;
      if (inp.newString != null) h += `<div class="label">new</div>${preBox(inp.newString)}`;
    } else if (t.tool === "write") {
      if (inp.filePath) h += `<div class="label">wrote</div>${preBox(inp.filePath)}`;
      if (inp.content)  h += `<div class="label">content</div>${preBox(inp.content)}`;
    } else if (t.tool === "todowrite") {
      const todos = inp.todos || [];
      h += `<div class="label">todos</div>` + todos.map(td => {
        const g = td.status === "completed" ? "[x]" : td.status === "in_progress" ? "[~]" : "[ ]";
        return `<div class="todo ${esc(td.status || "pending")}"><span class="box">${g}</span><span class="txt">${esc(td.content || "")}</span></div>`;
      }).join("");
    } else if (t.tool === "task") {
      if (inp.subagent_type) h += kv("subagent", inp.subagent_type);
      if (inp.description)   h += kv("description", inp.description);
      if (inp.prompt) h += `<div class="label">prompt</div>${preBox(inp.prompt)}`;
      if (out)        h += `<div class="label">result</div>${preBox(out)}`;
    } else if (t.tool === "skill") {
      if (inp.name) h += kv("skill", inp.name);
      if (out)      h += `<div class="label">skill content</div>${preBox(out)}`;
    } else {
      for (const k of Object.keys(inp)) h += kv(k, inp[k]);
      if (out) h += `<div class="label">output</div>${preBox(out)}`;
    }
    return h || `<div class="empty">no body captured</div>`;
  }

  function renderChat() {
    const totals = chat.totals || {};
    // EXTRA role only present when the run was configured with an extra
    // reviewer; back-compat surface for older runs stays AGENT/SIM only.
    const roleOrder = totals.EXTRA ? ["AGENT", "SIM", "EXTRA"] : ["AGENT", "SIM"];
    const totalsRow = roleOrder.map(role => {
      const t = totals[role] || {};
      return `<div class="role ${role}">
        <span class="tag">${role}</span>
        <span><b>${fmt(t.msgs)}</b> msgs</span>
        <span><b>${fmt(t.tools)}</b> tools</span>
        <span><b>${ktok(t.tokens)}</b> tok</span>
        <span><b>${money(t.cost)}</b></span>
      </div>`;
    }).join("");

    const turnsHtml = (chat.turns || []).map(turn => {
      const role = turn.who;
      const text = turn.text || "";
      const trace = (turn.tools || []).length ? `
        <div class="chat-trace">
          <div class="trace-summary">
            <span class="caret">▸</span>
            <span><b>${turn.tools.length}</b> action${turn.tools.length > 1 ? "s" : ""}</span>
            <span class="chips">${esc(chips(turn.summary))}</span>
            ${turn.tokens ? `<span style="color:var(--text-dim)">· ${ktok(turn.tokens)} tok</span>` : ""}
            ${turn.cost ? `<span class="cost">· ${money(turn.cost)}</span>` : ""}
          </div>
          <div class="trace-body">
            ${turn.tools.map(t => `
              <div class="tool-card">
                <div class="head">
                  <span class="caret">▸</span>
                  <span class="tool-name ${esc(t.tool)}">${esc(t.tool)}</span>
                  <span class="tool-title">${esc(t.title || "")}</span>
                  <span class="status-dot ${statusClass(t.status)}"></span>
                </div>
                <div class="body">${toolBody(t)}</div>
              </div>`).join("")}
          </div>
        </div>` : "";

      return `
        <div class="chat-turn ${role}">
          <div class="who-line">
            <span class="who">${role}</span>
            <span class="when"${turn.ts_synthetic ? ' title="approximate — codex emits no per-event timestamps"' : ""}>${turn.ts_synthetic ? "~" : "+"}${clock(turn.rel || 0)}</span>
          </div>
          <div class="chat-bubble ${text.trim() ? "" : "empty"}">${esc(text)}</div>
          ${trace}
        </div>`;
    }).join("");

    chatSec.innerHTML = `
      <div class="chat-totals">
        ${totalsRow}
        <span class="duration">${clock(chat.duration)} total</span>
      </div>
      ${turnsHtml || '<div class="empty">No chat turns captured.</div>'}
    `;

    // Wire up expand/collapse on click.
    chatSec.querySelectorAll(".trace-summary").forEach(s => {
      s.onclick = () => s.parentElement.classList.toggle("open");
    });
    chatSec.querySelectorAll(".tool-card .head").forEach(h => {
      h.onclick = ev => { ev.stopPropagation(); h.parentElement.classList.toggle("open"); };
    });
  }
  renderChat();

  // ----- eval reports (raw JSON for now) -----

  const evalBlob = DATA.eval || null;
  const evalSec = document.getElementById("eval");
  if (!evalBlob) {
    evalSec.innerHTML = `<div class="empty">No eval reports for this run.</div>`;
  } else {
    evalSec.innerHTML = `<h2>eval reports (${Object.keys(evalBlob).length})</h2>` +
      Object.entries(evalBlob).map(([name, body]) => `
        <details class="collapse">
          <summary>
            <span>${esc(name)}</span>
            <span class="meta">${bytes(asJson(body).length)}</span>
          </summary>
          <div class="body"><pre>${esc(asJson(body))}</pre></div>
        </details>`).join("");
  }
})();
