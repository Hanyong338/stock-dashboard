const DATA_BASE = "./data";

const state = {
  summaries: [],
  crossMentions: [],
  screening: [],
  channels: [],
  selectedChannel: "",
};

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

  const makeTab = (label, value) => {
    const btn = document.createElement("button");
    btn.className = "channel-tab" + (state.selectedChannel === value ? " active" : "");
    btn.dataset.channel = value;
    btn.textContent = label;
    btn.addEventListener("click", () => {
      state.selectedChannel = value;
      document.querySelectorAll(".channel-tab").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      renderSummaries();
    });
    return btn;
  };

  box.appendChild(makeTab("전체", ""));
  for (const name of names) {
    box.appendChild(makeTab(name, name));
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

  const closeList = () => {
    if (inList) {
      html += "</ul>";
      inList = false;
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
      closeList();
      html += `<h4 class="report-heading">${inlineMd(headerMatch[1])}</h4>`;
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
  closeList();
  return html;
}

function buildCard(item, hotTickers) {
  const card = document.createElement("div");
  const old = isOlderThanADay(item.published);
  card.className = "card" + (old ? " old" : "");

  const head = document.createElement("div");
  head.className = "card-head";
  head.innerHTML = `
    <div>
      <div class="card-channel">${escapeHtml(item.channel)}</div>
      <div class="card-title">${escapeHtml(item.title)}</div>
    </div>
    <div class="card-time">${formatRelativeTime(item.published)}</div>
  `;

  const body = document.createElement("div");
  body.className = "card-body" + (old ? " collapsed" : "");

  const report = document.createElement("div");
  report.className = "report";
  report.innerHTML = markdownToHtml(item.report_markdown || "");
  body.appendChild(report);

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
  body.appendChild(tagRow);

  const link = document.createElement("a");
  link.className = "card-link";
  link.href = item.url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.textContent = "영상 보기 ↗";
  body.appendChild(link);

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
    list.innerHTML = `<p class="empty-state">아직 ${who} 요약된 영상이 없습니다. 파이프라인이 실행되면 이곳에 표시됩니다.</p>`;
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
    list.innerHTML = '<p class="empty-state">차트 패턴 스크리닝 기능은 아직 준비 중입니다. (다음 단계에서 추가 예정)</p>';
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

document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById(`tab-${btn.dataset.tab}`).classList.add("active");
  });
});

document.getElementById("refreshBtn").addEventListener("click", loadAll);

loadAll();
