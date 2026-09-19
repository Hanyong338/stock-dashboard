const DATA_BASE = "./data";

const CHANNEL_COLORS = ["#f59e0b", "#ef4444", "#8b5cf6", "#10b981", "#3b82f6", "#ec4899", "#14b8a6"];

const SECTION_MAP = [
  { emoji: "🌐", icon: "🌐", color: "var(--sec-macro)" },
  { emoji: "📊", icon: "📊", color: "var(--sec-sector)" },
  { emoji: "💡", icon: "💡", color: "var(--sec-insight)" },
  { emoji: "🛡️", icon: "🛡️", color: "var(--sec-action)" },
  { emoji: "🛡", icon: "🛡️", color: "var(--sec-action)" },
];

function matchSection(headingText) {
  return SECTION_MAP.find((s) => headingText.includes(s.emoji));
}

const state = {
  summaries: [],
  crossMentions: [],
  dailyPicks: { leading: [], watch: [] },
  marketBrief: { as_of: null, indices: [], sectors: [] },
  morningBrief: {},
  screening: [],
  channels: [],
  selectedChannel: "",
  selectedDate: "",
};

function channelColor(name) {
  const idx = state.channels.findIndex((c) => c.name === name);
  if (idx >= 0) return CHANNEL_COLORS[idx % CHANNEL_COLORS.length];
  let hash = 0;
  for (let i = 0; i < (name || "").length; i++) hash = (hash * 31 + name.charCodeAt(i)) >>> 0;
  return CHANNEL_COLORS[hash % CHANNEL_COLORS.length];
}

async function loadJSON(name) {
  const res = await fetch(`${DATA_BASE}/${name}?t=${Date.now()}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`${name} 로드 실패 (${res.status})`);
  return res.json();
}

function formatRelativeTime(iso) {
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso || "";
  const diffMs = Date.now() - d.getTime();
  const diffH = Math.floor(diffMs / 3600000);
  if (diffH < 1) return "방금 전";
  if (diffH < 24) return `${diffH}시간 전`;
  const diffD = Math.floor(diffH / 24);
  return `${diffD}일 전`;
}

function isOlderThanADay(iso) {
  // 오늘/어제 올라온 영상은 펼친 채로 둔다. "30시간 전"처럼 어제 것인데도 24시간을
  // 넘겼다는 이유로 접혀서 본문이 안 보이는 일을 막기 위해 달력 날짜 기준으로 판단한다.
  const d = new Date(iso);
  if (isNaN(d.getTime())) return false;
  const yesterday = new Date();
  yesterday.setDate(yesterday.getDate() - 1);
  return dateKeyLocal(iso) < dateKeyFromDate(yesterday);
}

function dayLabel(iso) {
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "날짜 미상";
  return d.toLocaleDateString("ko-KR", { month: "long", day: "numeric", weekday: "short" });
}

function dateKeyFromDate(d) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function dateKeyLocal(iso) {
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "";
  return dateKeyFromDate(d);
}

function last7DateKeys() {
  const keys = [];
  for (let i = 0; i < 7; i++) {
    const d = new Date();
    d.setDate(d.getDate() - i);
    keys.push(dateKeyFromDate(d));
  }
  return keys;
}

function dateTabLabel(key, idx) {
  if (idx === 0) return "오늘";
  if (idx === 1) return "어제";
  const d = new Date(`${key}T00:00:00`);
  return d.toLocaleDateString("ko-KR", { month: "numeric", day: "numeric", weekday: "short" });
}

function renderPickList(containerEl, items) {
  containerEl.innerHTML = "";
  for (const item of items) {
    const row = document.createElement("div");
    row.className = "pick-item";

    const sector = document.createElement("div");
    sector.className = "pick-sector";
    sector.textContent = item.sector;
    row.appendChild(sector);

    const tags = document.createElement("div");
    tags.className = "tag-row";
    for (const ticker of item.tickers || []) {
      const tag = document.createElement("span");
      tag.className = "tag";
      tag.textContent = ticker;
      tags.appendChild(tag);
    }
    row.appendChild(tags);
    row.title = `${item.channels.length}개 채널: ${item.channels.join(", ")}`;

    containerEl.appendChild(row);
  }
}

function renderDailyPicks() {
  const box = document.getElementById("dailyPicks");
  const leading = state.dailyPicks.leading || [];
  const watch = state.dailyPicks.watch || [];

  if (!leading.length && !watch.length) {
    box.hidden = true;
    return;
  }
  box.hidden = false;

  const leadingList = document.getElementById("leadingPicksList");
  const watchList = document.getElementById("watchPicksList");

  if (leading.length) {
    renderPickList(leadingList, leading);
  } else {
    leadingList.innerHTML = '<p class="picks-empty">오늘 뚜렷한 주도 섹터가 아직 없어요.</p>';
  }

  if (watch.length) {
    renderPickList(watchList, watch);
  } else {
    watchList.innerHTML = '<p class="picks-empty">오늘 특별한 주의 섹터가 아직 없어요.</p>';
  }
}

const STRENGTH_LABEL = { 3: "직접 연관", 2: "산업 연관", 1: "테마 연관" };

function strengthDots(n) {
  const filled = Math.max(1, Math.min(3, Number(n) || 1));
  return "●".repeat(filled) + "○".repeat(3 - filled);
}

function changeToneFromText(text) {
  const s = String(text || "");
  if (/^[+↑]|상승|급등/.test(s)) return "up";
  if (/^[-↓]|하락|급락/.test(s)) return "down";
  return "flat";
}

function mbriefSection(title, bodyNodes) {
  const wrap = document.createElement("div");
  const h = document.createElement("h3");
  h.className = "mbrief-h3";
  h.textContent = title;
  wrap.appendChild(h);
  bodyNodes.forEach((n) => wrap.appendChild(n));
  return wrap;
}

function renderMorningBrief() {
  const box = document.getElementById("morningBrief");
  const d = state.morningBrief || {};
  if (!d.video_id) {
    box.hidden = true;
    return;
  }
  box.hidden = false;

  document.getElementById("mbriefAsOf").textContent = d.as_of || "";
  document.getElementById("mbriefLink").href = d.url || "#";

  // 지수 스트립
  const idxBox = document.getElementById("mbriefIndices");
  idxBox.innerHTML = "";
  (d.indices || []).forEach((i) => {
    const card = document.createElement("div");
    card.className = "mbrief-idx";
    card.innerHTML = `<span class="mbrief-idx-name">${escapeHtml(i.name)}</span>
      <strong class="mbrief-idx-val">${escapeHtml(i.value)}</strong>
      <span class="mbrief-idx-chg ${changeToneFromText(i.change)}">${escapeHtml(i.change)}</span>`;
    idxBox.appendChild(card);
  });

  // AI 3줄 요약
  const sum = d.ai_summary || {};
  const sumBox = document.getElementById("mbriefSummary");
  sumBox.innerHTML = "";
  [
    ["미국장 핵심", sum.us_market],
    ["섹터 수급", sum.sector_flow],
    ["국내장 대응", sum.korea_impact],
  ]
    .filter(([, v]) => v)
    .forEach(([label, v]) => {
      const row = document.createElement("div");
      row.className = "mbrief-sum-row";
      row.innerHTML = `<span class="mbrief-sum-label">${label}</span><p>${inlineMd(escapeHtml(v))}</p>`;
      sumBox.appendChild(row);
    });

  // 경제지표
  const evBox = document.getElementById("mbriefEvents");
  evBox.innerHTML = "";
  const events = d.economic_events || [];
  if (events.length) {
    const table = document.createElement("div");
    table.className = "mbrief-table";
    table.innerHTML =
      `<div class="mbrief-tr mbrief-th"><span>지표</span><span>발표</span><span>예상</span><span>직전</span></div>` +
      events
        .map(
          (e) => `<div class="mbrief-tr">
            <span class="mbrief-ev-name">${escapeHtml(e.name)}</span>
            <span class="mbrief-ev-num strong">${escapeHtml(e.actual)}</span>
            <span class="mbrief-ev-num">${escapeHtml(e.forecast)}</span>
            <span class="mbrief-ev-num">${escapeHtml(e.previous)}</span>
            <p class="mbrief-ev-note">${inlineMd(escapeHtml(e.assessment))}</p>
          </div>`
        )
        .join("");
    evBox.appendChild(mbriefSection("📊 주요 경제지표", [table]));
  }

  // 간밤 뉴스
  const newsBox = document.getElementById("mbriefNews");
  newsBox.innerHTML = "";
  const news = d.news || [];
  if (news.length) {
    const list = document.createElement("div");
    list.className = "mbrief-news";
    list.innerHTML = news
      .map(
        (n) => `<div class="mbrief-news-item">
          <div class="mbrief-news-title">${inlineMd(escapeHtml(n.title))}</div>
          <p class="mbrief-news-fact">${inlineMd(escapeHtml(n.fact))}</p>
          <p class="mbrief-news-comment">💬 ${inlineMd(escapeHtml(n.comment))}</p>
        </div>`
      )
      .join("");
    newsBox.appendChild(mbriefSection(`📰 간밤 핵심 뉴스 (${news.length})`, [list]));
  }

  // Overnight -> Korea
  const connBox = document.getElementById("mbriefConnections");
  connBox.innerHTML = "";
  const conns = d.connections || [];
  if (conns.length) {
    const list = document.createElement("div");
    list.className = "mbrief-conns";
    list.innerHTML = conns
      .map((c) => {
        const up = c.direction === "up";
        const picks = (c.korea_picks || [])
          .map(
            (p) => `<li>
              <span class="mbrief-dots s${Math.max(1, Math.min(3, Number(p.strength) || 1))}"
                    title="${STRENGTH_LABEL[p.strength] || ""}">${strengthDots(p.strength)}</span>
              <span class="mbrief-kname">${escapeHtml(p.name)}</span>
              <span class="mbrief-kreason">${inlineMd(escapeHtml(p.reason))}</span>
            </li>`
          )
          .join("");
        return `<div class="mbrief-conn ${up ? "is-up" : "is-down"}">
          <div class="mbrief-conn-head">
            <span class="mbrief-badge">${up ? "🔥 급등" : "⚠️ 급락"}</span>
            <span class="mbrief-conn-sector">${escapeHtml(c.sector)}</span>
            <span class="mbrief-chip">${escapeHtml(c.sector_class)}</span>
          </div>
          <div class="mbrief-us">
            <span class="mbrief-us-name">${escapeHtml(c.us_name)}</span>
            <span class="mbrief-ticker">${escapeHtml(c.us_ticker)}</span>
            <span class="mbrief-us-chg ${up ? "up" : "down"}">${escapeHtml(c.us_change)}</span>
          </div>
          <p class="mbrief-cause">${inlineMd(escapeHtml(c.cause))}</p>
          <p class="mbrief-logic"><span>연결 로직</span>${inlineMd(escapeHtml(c.logic))}</p>
          <ul class="mbrief-picks">${picks}</ul>
        </div>`;
      })
      .join("");
    connBox.appendChild(mbriefSection("🔗 미국 특징주 → 국내 연관주", [list]));
  }

  // 오늘 체크리스트
  const clBox = document.getElementById("mbriefChecklist");
  clBox.innerHTML = "";
  const caution = d.checklist_caution || [];
  const watch = d.checklist_watch || [];
  if (caution.length || watch.length) {
    const list = document.createElement("div");
    list.className = "mbrief-check";
    const item = (c, kind) => `<div class="mbrief-check-item ${kind}">
        <div class="mbrief-check-head">${kind === "caution" ? "🚨 주의" : "🔍 주목"} · ${escapeHtml(c.theme)}</div>
        <p class="mbrief-check-us">${escapeHtml(c.us)}</p>
        <p class="mbrief-check-cause">${inlineMd(escapeHtml(c.cause))}</p>
        <p class="mbrief-check-action"><span>대응</span>${inlineMd(escapeHtml(c.action))}</p>
      </div>`;
    list.innerHTML = caution.map((c) => item(c, "caution")).join("") + watch.map((c) => item(c, "watch")).join("");
    clBox.appendChild(mbriefSection("✅ 오늘 국내시장 체크리스트", [list]));
  }
}

function renderBriefingEmptyState() {
  // 미장 브리핑과 섹터 박스가 둘 다 비면 Market Briefing 탭이 빈 화면이 되므로 안내를 띄운다.
  const brief = document.getElementById("marketBrief");
  const picks = document.getElementById("dailyPicks");
  const morning = document.getElementById("morningBrief");
  document.getElementById("briefingEmpty").hidden = !(brief.hidden && picks.hidden && morning.hidden);
}

function formatPrice(n) {
  if (typeof n !== "number") return "-";
  return n.toLocaleString("en-US", { maximumFractionDigits: 2 });
}

function formatChangePercent(n) {
  if (typeof n !== "number") return "-";
  const sign = n > 0 ? "+" : "";
  return `${sign}${n.toFixed(2)}%`;
}

function changeDirClass(n) {
  if (typeof n !== "number" || n === 0) return "flat";
  return n > 0 ? "up" : "down";
}

function renderMarketBrief() {
  const section = document.getElementById("marketBrief");
  const indices = state.marketBrief.indices || [];
  const sectors = state.marketBrief.sectors || [];

  if (!indices.length && !sectors.length) {
    section.hidden = true;
    return;
  }
  section.hidden = false;

  document.getElementById("marketAsOf").textContent = state.marketBrief.as_of
    ? formatRelativeTime(state.marketBrief.as_of) + " 기준"
    : "";

  const indicesBox = document.getElementById("marketIndices");
  indicesBox.innerHTML = "";
  for (const idx of indices) {
    const card = document.createElement("div");
    card.className = "index-card " + changeDirClass(idx.change_percent);
    card.innerHTML = `
      <div class="index-name">${escapeHtml(idx.name)}</div>
      <div class="index-price">${formatPrice(idx.price)}</div>
      <div class="index-change">${formatChangePercent(idx.change_percent)}</div>
    `;
    indicesBox.appendChild(card);
  }

  const maxAbs = Math.max(1, ...sectors.map((s) => Math.abs(s.change_percent || 0)));
  const sectorsBox = document.getElementById("marketSectors");
  sectorsBox.innerHTML = "";
  for (const sec of sectors) {
    const row = document.createElement("div");
    row.className = "sector-row " + changeDirClass(sec.change_percent);
    const pct = Math.min(100, (Math.abs(sec.change_percent || 0) / maxAbs) * 100);
    row.innerHTML = `
      <span class="sector-name">${escapeHtml(sec.name)}</span>
      <span class="sector-bar-track"><span class="sector-bar-fill" style="width:${pct}%"></span></span>
      <span class="sector-change">${formatChangePercent(sec.change_percent)}</span>
    `;
    sectorsBox.appendChild(row);
  }
}

function renderChannelTabs() {
  const box = document.getElementById("channelTabs");
  box.innerHTML = "";

  const fromConfig = state.channels.map((c) => c.name);
  const fromSummaries = [...new Set(state.summaries.map((s) => s.channel))];
  const names = [...new Set([...fromConfig, ...fromSummaries])];

  const counts = {};
  for (const s of state.summaries) counts[s.channel] = (counts[s.channel] || 0) + 1;

  const makeTab = (label, value, color) => {
    const btn = document.createElement("button");
    btn.className = "channel-tab" + (state.selectedChannel === value ? " active" : "");
    btn.dataset.channel = value;

    if (color) {
      const dot = document.createElement("span");
      dot.className = "channel-dot";
      dot.style.setProperty("--dot-color", color);
      btn.appendChild(dot);
    }
    const text = document.createElement("span");
    text.textContent = label;
    btn.appendChild(text);

    const count = value ? counts[value] || 0 : state.summaries.length;
    if (count > 0) {
      const badge = document.createElement("span");
      badge.className = "channel-count";
      badge.textContent = count;
      btn.appendChild(badge);
    }

    btn.addEventListener("click", () => {
      state.selectedChannel = value;
      document.querySelectorAll(".channel-tab").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      renderSummaries();
    });
    return btn;
  };

  box.appendChild(makeTab("전체", "", null));
  for (const name of names) {
    box.appendChild(makeTab(name, name, channelColor(name)));
  }
}

function renderDateTabs() {
  const box = document.getElementById("dateTabs");
  box.innerHTML = "";

  const counts = {};
  for (const s of state.summaries) {
    const key = dateKeyLocal(s.published);
    counts[key] = (counts[key] || 0) + 1;
  }

  const makeTab = (label, value) => {
    const btn = document.createElement("button");
    btn.className = "date-tab" + (state.selectedDate === value ? " active" : "");
    btn.dataset.date = value;

    const text = document.createElement("span");
    text.textContent = label;
    btn.appendChild(text);

    const count = value ? counts[value] || 0 : state.summaries.length;
    if (count > 0) {
      const badge = document.createElement("span");
      badge.className = "channel-count";
      badge.textContent = count;
      btn.appendChild(badge);
    }

    btn.addEventListener("click", () => {
      state.selectedDate = value;
      document.querySelectorAll(".date-tab").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      renderSummaries();
    });
    return btn;
  };

  box.appendChild(makeTab("전체 기간", ""));
  last7DateKeys().forEach((key, idx) => {
    box.appendChild(makeTab(dateTabLabel(key, idx), key));
  });
}

function crossTickerSet() {
  return new Set(state.crossMentions.map((c) => c.ticker));
}

function inlineMd(escapedText) {
  return escapedText.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
}

function markdownToHtml(raw) {
  const escaped = escapeHtml(raw || "");
  const lines = escaped.split(/\r?\n/);
  let html = "";
  let inSection = false;
  const stack = []; // 열려 있는 목록들 [{ tag, indent, liOpen }] — 들여쓰기 깊이별로 중첩시킨다

  const openList = (tag, indent) => {
    html += `<${tag}>`;
    stack.push({ tag, indent, liOpen: false });
  };
  const closeTopList = () => {
    const top = stack.pop();
    if (top.liOpen) html += "</li>";
    html += `</${top.tag}>`;
  };
  const closeList = () => {
    while (stack.length) closeTopList();
  };
  const addItem = (tag, indent, text) => {
    while (stack.length && stack[stack.length - 1].indent > indent) closeTopList();

    if (!stack.length || stack[stack.length - 1].indent < indent) {
      openList(tag, indent); // 바로 위 <li> 안쪽에 열려서 계단식으로 들어간다
    } else {
      const top = stack[stack.length - 1];
      if (top.tag !== tag) {
        closeTopList();
        openList(tag, indent);
      } else if (top.liOpen) {
        html += "</li>";
        top.liOpen = false;
      }
    }
    html += `<li>${inlineMd(text)}`;
    stack[stack.length - 1].liOpen = true;
  };
  const closeSection = () => {
    closeList();
    if (inSection) {
      html += "</div>";
      inSection = false;
    }
  };

  for (const rawLine of lines) {
    const line = rawLine.trim();
    if (!line) continue; // 빈 줄이 있다고 목록을 끊지 않는다

    const headerMatch = line.match(/^#{1,6}\s+(.*)/);
    if (headerMatch) {
      closeSection();
      const headingText = headerMatch[1];
      const sec = matchSection(headingText);
      const secColorStyle = sec ? ` style="--sec-color:${sec.color}"` : "";
      const headingWithoutEmoji = sec ? headingText.replace(sec.emoji, "").trim() : headingText;
      html += `<div class="report-section"${secColorStyle}>`;
      html += `<div class="report-section-head">`;
      if (sec) html += `<span class="report-icon">${sec.icon}</span>`;
      html += `<h4 class="report-heading">${inlineMd(headingWithoutEmoji)}</h4></div>`;
      inSection = true;
      continue;
    }

    const bulletMatch = line.match(/^[-*]\s+(.*)/);
    const numberMatch = line.match(/^\d+[.)]\s+(.*)/);
    if (bulletMatch || numberMatch) {
      const indent = rawLine.match(/^\s*/)[0].replace(/\t/g, "  ").length;
      if (bulletMatch) addItem("ul", indent, bulletMatch[1]);
      else addItem("ol", indent, numberMatch[1]);
      continue;
    }

    closeList();
    html += `<p>${inlineMd(line)}</p>`;
  }
  closeSection();
  return html;
}

function isFresh(iso) {
  const d = new Date(iso);
  if (isNaN(d.getTime())) return false;
  return Date.now() - d.getTime() < 3 * 3600 * 1000;
}

function buildCard(item) {
  const card = document.createElement("div");
  const old = isOlderThanADay(item.published);
  card.className = "card" + (old ? " old" : "");
  const color = channelColor(item.channel);
  card.style.setProperty("--ch-color", color);

  const initial = (item.channel || "?").trim().charAt(0);
  const head = document.createElement("div");
  head.className = "card-head";
  head.innerHTML = `
    <div class="card-head-main">
      <div class="card-avatar">${escapeHtml(initial)}</div>
      <div class="card-title-group">
        <div class="card-channel">${escapeHtml(item.channel)}</div>
        <div class="card-title-row">
          <span class="card-title">${escapeHtml(item.title)}</span>
          <a class="yt-link" href="${item.url}" target="_blank" rel="noopener noreferrer" title="유튜브에서 영상 보기">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor"><path d="M21.6 7.2s-.21-1.5-.87-2.16c-.83-.87-1.76-.87-2.19-.92C15.44 4 12 4 12 4h-.01s-3.44 0-6.53.12c-.43.05-1.36.05-2.19.92C2.6 5.7 2.4 7.2 2.4 7.2S2.18 8.97 2.18 10.73v1.65c0 1.76.22 3.53.22 3.53s.21 1.5.87 2.16c.83.87 1.92.84 2.4.93 1.75.17 7.42.22 7.42.22s3.44-.01 6.53-.13c.43-.05 1.36-.05 2.19-.92.66-.66.87-2.16.87-2.16s.22-1.76.22-3.53v-1.65c0-1.76-.22-3.53-.22-3.53zM9.98 14.5v-5.5l5.27 2.76-5.27 2.74z"/></svg>
            영상보기
          </a>
        </div>
      </div>
    </div>
    <div class="card-time">
      ${isFresh(item.published) ? '<span class="new-badge">NEW</span>' : ""}
      <span>${formatRelativeTime(item.published)}</span>
      <svg class="chevron" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>
    </div>
  `;

  const body = document.createElement("div");
  body.className = "card-body" + (old ? " collapsed" : "");

  const inner = document.createElement("div");
  inner.className = "card-body-inner";

  const report = document.createElement("div");
  report.className = "report";
  report.innerHTML = markdownToHtml(item.report_markdown || "");
  inner.appendChild(report);

  body.appendChild(inner);
  head.addEventListener("click", () => body.classList.toggle("collapsed"));
  head.querySelector(".yt-link")?.addEventListener("click", (e) => e.stopPropagation());

  card.appendChild(head);

  if (item.key_summary) {
    const lead = document.createElement("div");
    lead.className = "card-lead";
    lead.innerHTML = inlineMd(escapeHtml(item.key_summary));
    card.appendChild(lead);
  }

  card.appendChild(body);
  return card;
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str ?? "";
  return div.innerHTML;
}

function renderSummaries() {
  const list = document.getElementById("summaryList");
  list.innerHTML = "";

  const filtered = state.summaries.filter(
    (s) =>
      (!state.selectedChannel || s.channel === state.selectedChannel) &&
      (!state.selectedDate || dateKeyLocal(s.published) === state.selectedDate)
  );

  if (!filtered.length) {
    const who = state.selectedChannel ? `"${escapeHtml(state.selectedChannel)}" 채널의` : "";
    list.innerHTML = `<div class="empty-state"><span class="empty-icon">🗂️</span>아직 ${who} 요약된 영상이 없습니다.<br>새 영상이 올라오면 1시간 내로 이곳에 표시됩니다.</div>`;
    return;
  }

  let lastDay = null;

  for (const item of filtered) {
    const label = dayLabel(item.published);
    if (label !== lastDay) {
      const dayHeader = document.createElement("div");
      dayHeader.className = "day-group";
      dayHeader.textContent = label;
      list.appendChild(dayHeader);
      lastDay = label;
    }
    list.appendChild(buildCard(item));
  }
}

function renderScreening() {
  const list = document.getElementById("screeningList");
  list.innerHTML = "";

  if (!state.screening.length) {
    list.innerHTML = '<div class="empty-state"><span class="empty-icon">📉</span>차트 패턴 스크리닝 기능은 아직 준비 중입니다.<br>다음 단계에서 추가될 예정이에요.</div>';
    return;
  }

  const hotTickers = crossTickerSet();
  for (const item of state.screening) {
    const card = document.createElement("div");
    card.className = "card";
    const isHot = hotTickers.has(item.name);
    card.innerHTML = `
      <div class="card-head">
        <div>
          <div class="card-title">${escapeHtml(item.name)} ${isHot ? '<span class="tag ticker-hit">유튜브 공통 언급</span>' : ""}</div>
        </div>
        <div class="card-time">${item.price ?? ""}</div>
      </div>
      <div class="tag-row">${(item.matched_patterns || []).map((p) => `<span class="tag">${escapeHtml(p)}</span>`).join("")}</div>
    `;
    list.appendChild(card);
  }
}

function setLastUpdated() {
  const el = document.getElementById("lastUpdated");
  const latest = state.summaries[0]?.fetched_at;
  el.textContent = latest ? `마지막 업데이트: ${formatRelativeTime(latest)}` : "데이터 없음";
}

async function loadAll() {
  try {
    const [summaries, crossMentions, dailyPicks, marketBrief, morningBrief, screening, channels] = await Promise.all([
      loadJSON("summaries.json"),
      loadJSON("cross_mentions.json"),
      loadJSON("daily_picks.json").catch(() => ({ leading: [], watch: [] })),
      loadJSON("market_brief.json").catch(() => ({ as_of: null, indices: [], sectors: [] })),
      loadJSON("morning_brief.json").catch(() => ({})),
      loadJSON("screening.json"),
      loadJSON("channels.json").catch(() => []),
    ]);
    state.summaries = summaries;
    state.crossMentions = crossMentions;
    state.dailyPicks = dailyPicks;
    state.marketBrief = marketBrief;
    state.morningBrief = morningBrief;
    state.screening = screening;
    state.channels = channels;

    renderMarketBrief();
    renderMorningBrief();
    renderDailyPicks();
    renderBriefingEmptyState();
    renderChannelTabs();
    renderDateTabs();
    renderSummaries();
    renderScreening();
    setLastUpdated();
  } catch (err) {
    document.getElementById("summaryList").innerHTML = `<p class="empty-state">데이터를 불러오지 못했습니다: ${escapeHtml(err.message)}</p>`;
  }
}

const sectionTabBtns = [...document.querySelectorAll(".tab-btn")];

function moveTabIndicator(btn) {
  const indicator = document.getElementById("tabIndicator");
  indicator.style.width = `${btn.offsetWidth}px`;
  indicator.style.transform = `translateX(${btn.offsetLeft}px)`;
}

window.addEventListener("resize", () => {
  const active = sectionTabBtns.find((b) => b.classList.contains("active"));
  if (active) moveTabIndicator(active);
});

sectionTabBtns.forEach((btn) => {
  btn.addEventListener("click", () => {
    sectionTabBtns.forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById(`tab-${btn.dataset.tab}`).classList.add("active");
    moveTabIndicator(btn);
  });
});
moveTabIndicator(sectionTabBtns[0]);

document.getElementById("refreshBtn").addEventListener("click", () => {
  const btn = document.getElementById("refreshBtn");
  btn.classList.add("spinning");
  loadAll().finally(() => setTimeout(() => btn.classList.remove("spinning"), 600));
});

function renderSkeleton() {
  const list = document.getElementById("summaryList");
  list.innerHTML = Array(3)
    .fill(0)
    .map(
      () => `
      <div class="skeleton-card">
        <div class="skeleton-line" style="width:35%"></div>
        <div class="skeleton-line" style="width:70%;height:16px"></div>
        <div class="skeleton-line" style="width:95%"></div>
        <div class="skeleton-line" style="width:85%"></div>
      </div>`
    )
    .join("");
}

renderSkeleton();
loadAll();
