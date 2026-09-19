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
  screening: [],
  channels: [],
  selectedChannel: "",
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

function renderCrossMentions() {
  const box = document.getElementById("crossMentions");
  const list = document.getElementById("crossList");
  list.innerHTML = "";

  if (!state.crossMentions.length) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  for (const item of state.crossMentions) {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.textContent = item.ticker;
    const count = document.createElement("span");
    count.className = "count";
    count.textContent = `${item.channels.length}개 채널`;
    chip.appendChild(count);
    chip.title = item.channels.join(", ");
    list.appendChild(chip);
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
        <div class="card-title">${escapeHtml(item.title)}</div>
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

  const link = document.createElement("a");
  link.className = "card-link";
  link.href = item.url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.textContent = "영상 보기 ↗";
  inner.appendChild(link);

  body.appendChild(inner);
  head.addEventListener("click", () => body.classList.toggle("collapsed"));

  card.appendChild(head);
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
    (s) => !state.selectedChannel || s.channel === state.selectedChannel
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
    const [summaries, crossMentions, screening, channels] = await Promise.all([
      loadJSON("summaries.json"),
      loadJSON("cross_mentions.json"),
      loadJSON("screening.json"),
      loadJSON("channels.json").catch(() => []),
    ]);
    state.summaries = summaries;
    state.crossMentions = crossMentions;
    state.screening = screening;
    state.channels = channels;

    renderCrossMentions();
    renderChannelTabs();
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
