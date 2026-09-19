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
  const d = new Date(iso);
  if (isNaN(d.getTime())) return false;
  return Date.now() - d.getTime() > 24 * 3600 * 1000;
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
  let inList = false;
  let inSection = false;

  const closeList = () => {
    if (inList) {
      html += "</ul>";
      inList = false;
    }
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
    if (!line) {
      closeList();
      continue;
    }

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

    const listMatch = line.match(/^[-*]\s+(.*)/);
    if (listMatch) {
      if (!inList) {
        html += "<ul>";
        inList = true;
      }
      html += `<li>${inlineMd(listMatch[1])}</li>`;
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

function buildCard(item, hotTickers) {
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

  const tagRow = document.createElement("div");
  tagRow.className = "tag-row";
  for (const ticker of item.tickers || []) {
    const tag = document.createElement("span");
    tag.className = "tag" + (hotTickers.has(ticker) ? " ticker-hit" : "");
    tag.textContent = ticker;
    tagRow.appendChild(tag);
  }
  for (const kw of item.keywords || []) {
    const tag = document.createElement("span");
    tag.className = "tag";
    tag.textContent = `#${kw}`;
    tagRow.appendChild(tag);
  }
  inner.appendChild(tagRow);

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

  const hotTickers = crossTickerSet();
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
    list.appendChild(buildCard(item, hotTickers));
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
    const [summaries, crossMentions, dailyPicks, screening, channels] = await Promise.all([
      loadJSON("summaries.json"),
      loadJSON("cross_mentions.json"),
      loadJSON("daily_picks.json").catch(() => ({ leading: [], watch: [] })),
      loadJSON("screening.json"),
      loadJSON("channels.json").catch(() => []),
    ]);
    state.summaries = summaries;
    state.crossMentions = crossMentions;
    state.dailyPicks = dailyPicks;
    state.screening = screening;
    state.channels = channels;

    renderDailyPicks();
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
  const idx = sectionTabBtns.indexOf(btn);
  indicator.style.transform = `translateX(${idx * 100}%)`;
}

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
