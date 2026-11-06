const state = { meta: null, report: null, view: "overview", token: sessionStorage.getItem("statsToken") || "" };
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const number = new Intl.NumberFormat();

async function api(path) {
  let response;
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      response = await fetch(path, { headers: state.token ? { "X-Stats-Token": state.token } : {} });
      break;
    } catch (error) {
      if (attempt === 1) throw error;
      await new Promise(resolve => setTimeout(resolve, 200));
    }
  }
  if (response.status === 401) {
    $("#auth-dialog").showModal();
    throw new Error("Enter the dashboard access token to continue.");
  }
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || "Unable to load statistics.");
  return payload;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);
}

function formatDate(timestamp) {
  if (!timestamp) return "—";
  return new Date(timestamp * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

function labelKind(kind) {
  return ({ forum_post: "Forum post", announcement: "Announcement", thread: "Thread", text: "Text", voice_text: "Voice text" })[kind] || kind;
}

function rangeParams() {
  const period = $("#period").value;
  if (period === "all") return {};
  if (period === "custom") {
    const from = $("#date-from").value;
    const to = $("#date-to").value;
    return {
      start: from ? Math.floor(new Date(`${from}T00:00:00`).getTime() / 1000) : undefined,
      end: to ? Math.floor(new Date(`${to}T00:00:00`).getTime() / 1000) + 86400 : undefined,
    };
  }
  return { start: Math.floor(Date.now() / 1000) - Number(period) * 86400 };
}

function reportUrl() {
  const params = new URLSearchParams({ guild_id: $("#guild").value });
  Object.entries(rangeParams()).forEach(([key, value]) => value && params.set(key, value));
  const scope = $("#scope").value;
  if (scope) {
    const [type, id] = scope.split(":");
    params.set(type === "forum" ? "forum_id" : "channel_id", id);
  }
  const member = $("#member").value.match(/(\d{15,22})/);
  if (member) params.set("member_id", member[1]);
  return `/api/report?${params}`;
}

function populateMeta() {
  const guild = $("#guild");
  guild.innerHTML = state.meta.guilds.map(item => `<option value="${item.guild_id}">${escapeHtml(item.name)} · ${number.format(item.messages)}</option>`).join("");
  populateScopes();
  populateMembers();
}

function populateScopes() {
  const guildId = $("#guild").value;
  const items = state.meta.channels.filter(item => item.guild_id === guildId && ["text", "voice_text", "announcement", "forum_post", "forum"].includes(item.kind));
  const groups = [
    ["forum", "Forums"], ["forum_post", "Forum posts"], ["text", "Text channels"],
    ["voice_text", "Voice-channel text"], ["announcement", "Announcements"],
  ];
  $("#scope").innerHTML = `<option value="">All locations</option>` + groups.map(([kind, title]) => {
    const options = items.filter(item => item.kind === kind).map(item => {
      const prefix = item.parent_name ? `${item.parent_name} / ` : "";
      const type = kind === "forum" ? "forum" : "channel";
      return `<option value="${type}:${item.channel_id}">${escapeHtml(prefix + item.name)}</option>`;
    }).join("");
    return options ? `<optgroup label="${title}">${options}</optgroup>` : "";
  }).join("");
}

function populateMembers() {
  const guildId = $("#guild").value;
  $("#member-options").innerHTML = state.meta.members.filter(item => item.guild_id === guildId).map(item =>
    `<option value="${escapeHtml(item.display_name)} · ${item.user_id}"></option>`
  ).join("");
}

function renderTimeline(rows) {
  const root = $("#timeline");
  if (!rows.length) { root.innerHTML = "No activity in this range."; return; }
  const width = 1000, height = 280, left = 52, right = 16, top = 12, bottom = 36;
  const max = Math.max(...rows.map(row => row.messages), 1);
  const x = index => left + index * (width - left - right) / Math.max(rows.length - 1, 1);
  const y = value => top + (height - top - bottom) * (1 - value / max);
  const points = rows.map((row, index) => `${x(index)},${y(row.messages)}`).join(" ");
  const area = `${left},${height-bottom} ${points} ${x(rows.length-1)},${height-bottom}`;
  const grid = [0, .25, .5, .75, 1].map(ratio => {
    const py = y(max * ratio);
    return `<line class="grid-line" x1="${left}" y1="${py}" x2="${width-right}" y2="${py}"/><text class="axis-label" x="0" y="${py+4}">${number.format(Math.round(max*ratio))}</text>`;
  }).join("");
  const labelIndexes = [...new Set([0, Math.floor((rows.length-1)/2), rows.length-1])];
  const labels = labelIndexes.map(index => `<text class="axis-label" text-anchor="${index === 0 ? 'start' : index === rows.length-1 ? 'end' : 'middle'}" x="${x(index)}" y="${height-8}">${escapeHtml(rows[index].bucket)}</text>`).join("");
  const dots = rows.length <= 45 ? rows.map((row,index) => `<circle class="series-dot" cx="${x(index)}" cy="${y(row.messages)}" r="3"><title>${escapeHtml(row.bucket)}: ${number.format(row.messages)} messages</title></circle>`).join("") : "";
  root.innerHTML = `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none"><defs><linearGradient id="area-gradient" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="#4de1d1" stop-opacity=".28"/><stop offset="1" stop-color="#4de1d1" stop-opacity="0"/></linearGradient></defs>${grid}<polygon class="area" points="${area}"/><polyline class="series" points="${points}"/>${dots}${labels}</svg>`;
}

function renderHeatmap(rows) {
  const values = new Map(rows.map(row => [`${row.weekday}:${row.hour}`, row.messages]));
  const max = Math.max(...rows.map(row => row.messages), 1);
  const days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  let html = `<span></span>${Array.from({length:24},(_,hour) => `<span class="heat-hour">${hour % 3 === 0 ? hour : ""}</span>`).join("")}`;
  days.forEach((day, weekday) => {
    html += `<span class="heat-label">${day}</span>`;
    for (let hour = 0; hour < 24; hour++) {
      const count = values.get(`${weekday}:${hour}`) || 0;
      const alpha = count ? .12 + .88 * Math.sqrt(count / max) : 0;
      html += `<span class="heat-cell" style="background:rgba(77,225,209,${alpha})" title="${day} ${String(hour).padStart(2,'0')}:00 UTC · ${number.format(count)}"></span>`;
    }
  });
  $("#heatmap").innerHTML = html;
}

function compactRows(rows, type) {
  return rows.slice(0, 7).map((row, index) => {
    const isMember = type === "member";
    const name = isMember ? row.name : row.name || `Channel ${row.channel_id}`;
    const sub = isMember ? `${number.format(row.locations)} locations` : `${labelKind(row.kind)} · ${number.format(row.contributors)} contributors`;
    const value = isMember ? row.user_id : `${row.kind === "forum_post" ? "channel" : "channel"}:${row.channel_id}`;
    return `<div class="rank-row"><span class="number">${String(index+1).padStart(2,"0")}</span><button data-drill="${isMember ? 'member' : 'scope'}" data-value="${value}">${escapeHtml(name)}<small>${escapeHtml(sub)}</small></button><strong>${number.format(row.messages)}</strong></div>`;
  }).join("") || `<div class="rank-row"><span></span><span>No activity in this range.</span></div>`;
}

function renderTables(report) {
  $("#top-members-compact").innerHTML = compactRows(report.top_members, "member");
  $("#top-locations-compact").innerHTML = compactRows(report.top_locations, "location");
  $("#members-table").innerHTML = report.top_members.map((row,index) => {
    const share = report.summary.messages ? row.messages / report.summary.messages * 100 : 0;
    return `<tr><td class="mono">${String(index+1).padStart(2,"0")}</td><td><button data-drill="member" data-value="${row.user_id}">${escapeHtml(row.name)}</button></td><td class="mono">${number.format(row.locations)}</td><td class="mono">${number.format(row.messages)}</td><td><span class="share-track"><i style="width:${Math.min(100,share)}%"></i></span>${share.toFixed(1)}%</td></tr>`;
  }).join("");
  $("#locations-table").innerHTML = report.top_locations.map((row,index) => `<tr><td class="mono">${String(index+1).padStart(2,"0")}</td><td><button data-drill="scope" data-value="channel:${row.channel_id}">${escapeHtml(row.parent_name ? row.parent_name + " / " + row.name : row.name)}</button></td><td><span class="type-pill">${escapeHtml(labelKind(row.kind))}</span></td><td class="mono">${number.format(row.contributors)}</td><td class="mono">${number.format(row.messages)}</td></tr>`).join("");
  $("#forums-table").innerHTML = report.top_forums.map((row,index) => `<tr><td class="mono">${String(index+1).padStart(2,"0")}</td><td><button data-drill="scope" data-value="forum:${row.forum_id}">${escapeHtml(row.name)}</button></td><td class="mono">${number.format(row.threads)}</td><td class="mono">${number.format(row.contributors)}</td><td class="mono">${number.format(row.messages)}</td></tr>`).join("");
  $("#forum-threads-table").innerHTML = report.forum_threads.map((row,index) => `<tr><td class="mono">${String(index+1).padStart(2,"0")}</td><td><button data-drill="scope" data-value="channel:${row.channel_id}">${escapeHtml(row.name)}</button></td><td>${escapeHtml(row.parent_name || "Unknown forum")}</td><td class="mono">${number.format(row.contributors)}</td><td class="mono">${number.format(row.messages)}</td></tr>`).join("") || `<tr><td colspan="5">No forum-thread activity in this range.</td></tr>`;
  $$('[data-drill="member"]').forEach(button => button.addEventListener("click", () => { $("#member").value = button.dataset.value; loadReport(); }));
  $$('[data-drill="scope"]').forEach(button => button.addEventListener("click", () => { $("#scope").value = button.dataset.value; loadReport(); }));
}

function render(report) {
  state.report = report;
  const summary = report.summary;
  $("#metric-messages").textContent = number.format(summary.messages);
  $("#metric-members").textContent = number.format(summary.contributors);
  $("#metric-locations").textContent = number.format(summary.locations);
  $("#metric-average").textContent = number.format(summary.average_per_active_day);
  $("#metric-range").textContent = summary.first_message_at ? `${formatDate(summary.first_message_at)} → ${formatDate(summary.last_message_at)}` : "selected range";
  $("#peak").textContent = summary.peak_bucket ? `Peak ${summary.peak_bucket.bucket} · ${number.format(summary.peak_bucket.messages)}` : "Peak —";
  $("#updated-at").textContent = `Updated ${new Date(report.generated_at * 1000).toLocaleTimeString()}`;
  renderTimeline(report.timeline);
  renderHeatmap(report.heatmap);
  renderTables(report);
  applyView();
}

function applyView() {
  const titles = {
    overview: ["Server pulse", "Overview"], messages: ["Temporal analysis", "Messages"],
    channels: ["Location drilldown", "Channels & threads"], members: ["Contributor drilldown", "Members"],
    forums: ["Thread-aware reporting", "Forums"],
  };
  $("#view-kicker").textContent = titles[state.view][0];
  $("#view-title").textContent = titles[state.view][1];
  $$('[data-section]').forEach(section => section.classList.toggle("view-visible", section.dataset.section.split(" ").includes(state.view)));
  $("#dashboard").classList.remove("hidden");
}

async function loadReport() {
  const applyButton = $("#apply");
  const originalLabel = applyButton.textContent;
  applyButton.disabled = true;
  applyButton.textContent = "Running…";
  $("#notice").classList.add("hidden");
  $("#loading").classList.remove("hidden");
  $("#dashboard").classList.add("hidden");
  try {
    const report = await api(reportUrl());
    render(report);
    return report;
  } catch (error) {
    $("#notice").textContent = error.message;
    $("#notice").classList.remove("hidden");
  } finally {
    $("#loading").classList.add("hidden");
    applyButton.disabled = false;
    applyButton.textContent = originalLabel;
  }
}

function registerWebMCP() {
  const context = document.modelContext;
  if (!context?.registerTool) return;
  const allowedViews = ["overview", "messages", "channels", "members", "forums"];
  Promise.resolve(context.registerTool({
    name: "run_message_stats_report",
    title: "Run message statistics report",
    description: "Run and display a message activity report for a server, channel, forum, forum post thread, or member.",
    inputSchema: {
      type: "object",
      properties: {
        view: { type: "string", enum: allowedViews },
        periodDays: { anyOf: [{ type: "integer", minimum: 1, maximum: 365 }, { type: "string", enum: ["all"] }] },
        scopeType: { type: "string", enum: ["channel", "forum"] },
        scopeId: { type: "string", pattern: "^[0-9]{15,22}$" },
        memberId: { type: "string", pattern: "^[0-9]{15,22}$" },
      },
      additionalProperties: false,
    },
    annotations: { readOnlyHint: true, untrustedContentHint: false },
    async execute(input = {}) {
      if (input.view && !allowedViews.includes(input.view)) throw new Error("Unsupported dashboard view.");
      if ((input.scopeType && !input.scopeId) || (!input.scopeType && input.scopeId)) throw new Error("scopeType and scopeId must be supplied together.");
      if (input.view) {
        state.view = input.view;
        $$('.nav-item').forEach(item => item.classList.toggle("active", item.dataset.view === state.view));
      }
      if (input.periodDays !== undefined) $("#period").value = String(input.periodDays);
      if (input.scopeType) $("#scope").value = `${input.scopeType}:${input.scopeId}`;
      if (input.memberId) $("#member").value = input.memberId;
      const report = await loadReport();
      return report ? { view: state.view, summary: report.summary, generatedAt: report.generated_at } : { error: "Report unavailable" };
    },
  })).catch(error => console.warn("WebMCP registration failed", error));
}

async function init() {
  try {
    state.meta = await api("/api/meta");
    if (!state.meta.guilds.length) throw new Error("No stored messages were found.");
    populateMeta();
    await loadReport();
  } catch (error) {
    $("#loading").classList.add("hidden");
    $("#notice").textContent = error.message;
    $("#notice").classList.remove("hidden");
  }
}

$$('.nav-item').forEach(button => button.addEventListener("click", () => {
  $$('.nav-item').forEach(item => item.classList.remove("active"));
  button.classList.add("active"); state.view = button.dataset.view; applyView();
}));
$("#guild").addEventListener("change", () => { populateScopes(); populateMembers(); });
$("#period").addEventListener("change", () => $("#custom-dates").classList.toggle("visible", $("#period").value === "custom"));
$("#apply").addEventListener("click", loadReport);
$("#refresh").addEventListener("click", loadReport);
$("#auth-form").addEventListener("submit", event => {
  event.preventDefault(); state.token = $("#token").value; sessionStorage.setItem("statsToken", state.token); $("#auth-dialog").close(); init();
});
registerWebMCP();
init();
