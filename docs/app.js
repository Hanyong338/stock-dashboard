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
  morningBrief: {},
  calendar: { months: [], events: [] },
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


const STRENGTH_LABEL = { 3: "직접 연관", 2: "산업 연관", 1: "테마 연관" };

function strengthDots(n) {
  const filled = Math.max(1, Math.min(3, Number(n) || 1));
  return "●".repeat(filled) + "○".repeat(3 - filled);
}

function changeToneFromText(text) {
  const s = String(text || "").trim();
  if (!s || s === "-") return "flat"; // 값 없음을 하락(▼)으로 오인하지 않게 한다
  if (/^[+↑]|상승|급등/.test(s)) return "up";
  if (/^[-−↓]/.test(s) || /하락|급락/.test(s)) return "down";
  return "flat";
}

// 색만으로 등락을 알리면 색약 사용자가 구분할 수 없어서 기호를 함께 붙인다.
const TONE_MARK = { up: "▲", down: "▼", flat: "－" };

// 방송에서 등락률을 안 밝힌 종목은 [계약] [수혜] 같은 상태 뱃지가 들어온다.
// 숫자가 아니므로 삼각형이나 등락 색을 붙이면 안 된다.
function isStateBadge(text) {
  return /^\s*\[[^\]]+\]\s*$/.test(String(text || ""));
}

function changeWithMark(text, tone) {
  if (isStateBadge(text)) return `<span class="state-badge">${escapeHtml(String(text).replace(/[\[\]]/g, ""))}</span>`;
  const mark = TONE_MARK[tone] || "";
  return `<span class="tri" aria-hidden="true">${mark}</span>${escapeHtml(text)}`;
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

/* ---------- 증시 캘린더 ---------- */

const CAL_CATEGORIES = [
  { key: "major", label: "주요 이벤트" },
  { key: "issue", label: "체크포인트" },
  { key: "macro", label: "경제지표" },
  { key: "earnings", label: "실적" },
  { key: "holiday", label: "휴장" },
];

const calState = { month: null, hidden: new Set(), selected: null };
const CAL_MAX_LANES = 3; // 달력 한 칸에 보여줄 최대 줄 수. 넘치면 +N 으로 접는다

function ymd(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function monthLabel(ym) {
  const [y, m] = ym.split("-");
  return `${y}년 ${Number(m)}월`;
}

function calEvents() {
  return (state.calendar.events || []).filter((e) => !calState.hidden.has(e.category));
}

/** 'YYYY-MM-DD' 를 로컬 자정으로 읽는다.
 *  new Date('2026-09-26') 는 UTC 자정이라 한국시간에서 9시간 밀려 비교가 틀어진다. */
function parseDay(s) {
  return new Date(s + "T00:00:00");
}

/** 한 주(7칸) 안에서 막대가 서로 겹치지 않도록 줄(lane)을 배정한다. */
function assignLanes(events, weekStart, weekEnd) {
  const placed = [];
  const lanes = [];

  const sorted = [...events].sort((a, b) => {
    if (a.start !== b.start) return a.start < b.start ? -1 : 1;
    const da = parseDay(a.end) - parseDay(a.start);
    const db = parseDay(b.end) - parseDay(b.start);
    return db - da; // 긴 일정을 위쪽 줄에 둔다
  });

  for (const ev of sorted) {
    const startsBefore = parseDay(ev.start) < weekStart;
    const endsAfter = parseDay(ev.end) > weekEnd;
    const s = startsBefore ? weekStart : parseDay(ev.start);
    const e = endsAfter ? weekEnd : parseDay(ev.end);
    const col = Math.round((s - weekStart) / 86400000);
    const span = Math.round((e - s) / 86400000) + 1;

    let lane = 0;
    while (true) {
      lanes[lane] = lanes[lane] || [];
      const clash = lanes[lane].some((p) => col < p.col + p.span && p.col < col + span);
      if (!clash) break;
      lane++;
    }
    lanes[lane].push({ col, span });
    placed.push({ ev, col, span, lane, continuesLeft: startsBefore, continuesRight: endsAfter });
  }
  return placed;
}

function renderCalendar() {
  const grid = document.getElementById("calGrid");
  const months = state.calendar.months || [];
  const empty = document.getElementById("calEmpty");

  if (!months.length) {
    grid.innerHTML = "";
    empty.hidden = false;
    document.getElementById("calMonthLabel").textContent = "데이터 없음";
    return;
  }
  empty.hidden = true;
  if (!calState.month || !months.includes(calState.month)) calState.month = months[0];

  document.getElementById("calMonthLabel").textContent = monthLabel(calState.month);
  document.getElementById("calPrev").disabled = months.indexOf(calState.month) === 0;
  document.getElementById("calNext").disabled = months.indexOf(calState.month) === months.length - 1;

  const [y, m] = calState.month.split("-").map(Number);
  const first = new Date(y, m - 1, 1);
  const gridStart = new Date(y, m - 1, 1 - first.getDay()); // 그 주 일요일부터
  const todayStr = ymd(new Date());
  const events = calEvents();

  grid.innerHTML = "";
  for (let w = 0; w < 6; w++) {
    const weekStart = new Date(gridStart.getFullYear(), gridStart.getMonth(), gridStart.getDate() + w * 7);
    const weekEnd = new Date(weekStart.getFullYear(), weekStart.getMonth(), weekStart.getDate() + 6);
    if (w >= 4 && weekStart.getMonth() !== m - 1 && weekEnd.getMonth() !== m - 1) break; // 빈 주는 그리지 않는다

    const week = document.createElement("div");
    week.className = "cal-week";

    const dates = document.createElement("div");
    dates.className = "cal-dates";
    for (let i = 0; i < 7; i++) {
      const day = new Date(weekStart.getFullYear(), weekStart.getMonth(), weekStart.getDate() + i);
      const key = ymd(day);
      const cell = document.createElement("button");
      cell.type = "button";
      cell.className =
        "cal-day" +
        (day.getMonth() !== m - 1 ? " other" : "") +
        (i === 0 ? " sun" : i === 6 ? " sat" : "") +
        (key === todayStr ? " today" : "") +
        (key === calState.selected ? " picked" : "");
      cell.innerHTML = `<span class="cal-dnum">${day.getDate()}</span>`;
      cell.addEventListener("click", () => {
        calState.selected = calState.selected === key ? null : key;
        renderCalendar();
      });
      dates.appendChild(cell);
    }
    week.appendChild(dates);

    const inWeek = events.filter((e) => e.end >= ymd(weekStart) && e.start <= ymd(weekEnd));
    const bars = document.createElement("div");
    bars.className = "cal-bars";
    const placed = assignLanes(inWeek, weekStart, weekEnd);

    // 지표가 몰리는 날은 하루 10건도 나온다. 달력에는 3개까지만 보이고 나머지는 +N 으로 접는다.
    const shown = placed.filter((p) => p.lane < CAL_MAX_LANES);
    const overflowByCol = {};
    for (const p of placed) {
      if (p.lane < CAL_MAX_LANES) continue;
      for (let c = p.col; c < p.col + p.span; c++) overflowByCol[c] = (overflowByCol[c] || 0) + 1;
    }
    const overflowCols = Object.keys(overflowByCol);
    const laneCount = Math.min(placed.reduce((mx, p) => Math.max(mx, p.lane + 1), 0), CAL_MAX_LANES) +
      (overflowCols.length ? 1 : 0);
    bars.style.setProperty("--lanes", laneCount);

    for (const col of overflowCols) {
      const more = document.createElement("div");
      more.className = "cal-more";
      more.style.gridColumn = `${Number(col) + 1} / span 1`;
      more.style.gridRow = String(CAL_MAX_LANES + 1);
      more.textContent = `+${overflowByCol[col]}`;
      const day = new Date(weekStart.getFullYear(), weekStart.getMonth(), weekStart.getDate() + Number(col));
      more.addEventListener("click", () => {
        calState.selected = ymd(day);
        renderCalendar();
      });
      bars.appendChild(more);
    }

    for (const p of shown) {
      const bar = document.createElement("div");
      bar.className =
        `cal-bar cat-${p.ev.category}` +
        (p.continuesLeft ? " cont-l" : "") +
        (p.continuesRight ? " cont-r" : "");
      bar.style.gridColumn = `${p.col + 1} / span ${p.span}`;
      bar.style.gridRow = String(p.lane + 1);
      bar.textContent = p.ev.title;
      bar.title = p.ev.detail ? `${p.ev.title} (${p.ev.detail})` : p.ev.title;
      bar.addEventListener("click", () => {
        calState.selected = p.ev.start;
        renderCalendar();
      });
      bars.appendChild(bar);
    }
    week.appendChild(bars);
    grid.appendChild(week);
  }

  renderCalFilters();
  renderCalAgenda();
}

function renderCalFilters() {
  const box = document.getElementById("calFilters");
  const counts = {};
  for (const e of state.calendar.events || []) counts[e.category] = (counts[e.category] || 0) + 1;

  box.innerHTML = "";
  for (const c of CAL_CATEGORIES) {
    if (!counts[c.key]) continue;
    const on = !calState.hidden.has(c.key);
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = `cal-chip cat-${c.key}` + (on ? " on" : "");
    btn.innerHTML = `<span class="cal-dot"></span>${c.label}`;
    btn.addEventListener("click", () => {
      if (on) calState.hidden.add(c.key);
      else calState.hidden.delete(c.key);
      renderCalendar();
    });
    box.appendChild(btn);
  }
}

function renderCalAgenda() {
  const box = document.getElementById("calAgenda");
  box.innerHTML = "";
  if (!calState.selected) return;

  const day = calEvents().filter((e) => e.start <= calState.selected && e.end >= calState.selected);
  const d = new Date(calState.selected + "T00:00:00");
  const head = document.createElement("div");
  head.className = "cal-agenda-head";
  head.textContent = d.toLocaleDateString("ko-KR", { month: "long", day: "numeric", weekday: "long" });
  box.appendChild(head);

  if (!day.length) {
    const p = document.createElement("p");
    p.className = "cal-agenda-empty";
    p.textContent = "이 날짜에 등록된 일정이 없어요.";
    box.appendChild(p);
    return;
  }

  for (const e of day) {
    const row = document.createElement("div");
    row.className = `cal-agenda-item cat-${e.category}`;
    row.innerHTML = `<span class="cal-dot"></span><span class="cal-agenda-title">${escapeHtml(e.title)}</span>` +
      (e.detail ? `<span class="cal-agenda-detail">${escapeHtml(e.detail)}</span>` : "");
    box.appendChild(row);
  }
}

function setupCalendarControls() {
  const menu = document.getElementById("calMonthMenu");
  const btn = document.getElementById("calMonthBtn");

  btn.addEventListener("click", () => {
    const months = state.calendar.months || [];
    menu.innerHTML = "";
    for (const ym of months) {
      const li = document.createElement("li");
      li.role = "option";
      li.className = "cal-monthitem" + (ym === calState.month ? " on" : "");
      li.textContent = monthLabel(ym);
      li.addEventListener("click", () => {
        calState.month = ym;
        calState.selected = null;
        menu.hidden = true;
        renderCalendar();
      });
      menu.appendChild(li);
    }
    menu.hidden = !menu.hidden;
  });

  document.addEventListener("click", (e) => {
    if (!menu.hidden && !menu.contains(e.target) && e.target !== btn && !btn.contains(e.target)) menu.hidden = true;
  });

  const step = (delta) => {
    const months = state.calendar.months || [];
    const i = months.indexOf(calState.month) + delta;
    if (i < 0 || i >= months.length) return;
    calState.month = months[i];
    calState.selected = null;
    renderCalendar();
  };
  document.getElementById("calPrev").addEventListener("click", () => step(-1));
  document.getElementById("calNext").addEventListener("click", () => step(1));
}

function renderTodayVerdict(d) {
  // 당잠사 3줄 요약은 '오늘 뭘 해야 하나'에 대한 답이라 첫 화면 맨 위에 둔다.
  const box = document.getElementById("todayVerdict");
  const sum = (d && d.ai_summary) || {};
  const rows = [
    ["미국장", sum.us_market],
    ["섹터", sum.sector_flow],
    ["국내 대응", sum.korea_impact],
  ].filter(([, v]) => v);

  if (!rows.length) {
    box.hidden = true;
    return;
  }
  box.hidden = false;

  const meta = [d.as_of, d.published ? formatRelativeTime(d.published) + " 방송" : ""].filter(Boolean).join(" · ");
  document.getElementById("verdictMeta").textContent = meta;

  const body = document.getElementById("verdictBody");
  body.innerHTML = rows
    .map(
      ([label, v]) => `<div class="verdict-row">
        <span class="verdict-label">${label}</span>
        <p>${inlineMd(escapeHtml(v))}</p>
      </div>`
    )
    .join("");
}

function renderMbriefNav(d) {
  // 리포트가 모바일에서 4,000px 넘게 길어서 섹션 점프가 없으면 뒷부분은 읽히지 않는다.
  const nav = document.getElementById("mbriefNav");
  const items = [
    ["mbriefIndices", "지표", (d.indices || []).length],
    ["mbriefEvents", "경제지표", (d.economic_events || []).length],
    ["mbriefNews", "뉴스", (d.news || []).length],
    ["mbriefSectors", "섹터", (d.sectors || []).length],
    ["mbriefConnections", "국내 연관주", (d.connections || []).length],
    ["mbriefChecklist", "체크리스트", (d.checklist_caution || []).length + (d.checklist_watch || []).length],
  ].filter(([, , n]) => n > 0);

  nav.innerHTML = "";
  items.forEach(([id, label, n]) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "mbrief-navchip";
    btn.innerHTML = `${label}<span class="mbrief-navcount num">${n}</span>`;
    btn.addEventListener("click", () => {
      const el = document.getElementById(id);
      if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
    });
    nav.appendChild(btn);
  });
}

function renderMorningBrief() {
  const box = document.getElementById("morningBrief");
  const d = state.morningBrief || {};
  if (!d.video_id) {
    box.hidden = true;
    document.getElementById("todayVerdict").hidden = true;
    return;
  }
  box.hidden = false;

  document.getElementById("mbriefAsOf").textContent = d.as_of || "";
  document.getElementById("mbriefLink").href = d.url || "#";

  // 지수는 시세 데이터가 정해진 순서(나스닥/S&P/다우/SOX/금리/달러/WTI)로 넣어주므로 그대로 쓴다.
  const indices = d.indices || [];
  const idxBox = document.getElementById("mbriefIndices");
  idxBox.innerHTML = "";
  idxBox.parentElement.querySelector(".mbrief-note")?.remove(); // 새로고침 시 중복 방지
  indices.forEach((i) => {
    const card = document.createElement("div");
    const tone = changeToneFromText(i.change);
    // 현재값과 등락률을 세트로 보여주는 게 원칙. 방송에 값이 없을 때만 등락률을 주인공으로 올린다.
    const hasValue = i.value && i.value !== "-";
    card.className = "mbrief-idx" + (hasValue ? "" : " no-value");
    card.innerHTML =
      `<span class="mbrief-idx-name">${escapeHtml(i.name)}</span>` +
      (hasValue ? `<strong class="mbrief-idx-val num">${escapeHtml(i.value)}</strong>` : "") +
      `<span class="mbrief-idx-chg num ${isStateBadge(i.change) ? "" : tone}">${changeWithMark(i.change, tone)}</span>`;
    idxBox.appendChild(card);
  });

  // 종가와 등락률의 출처가 다를 수 있어 밝혀둔다 (금융 데이터는 기준을 모르면 못 쓴다)
  if (d.indices_note) {
    const note = document.createElement("p");
    note.className = "mbrief-note";
    note.textContent = d.indices_note;
    idxBox.insertAdjacentElement("afterend", note);
  }

  renderTodayVerdict(d);
  renderMbriefNav(d);

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

  // 섹터 성과 (실제 SPDR 섹터 ETF 시세 기준. 방송 발언이 아님)
  const secBox = document.getElementById("mbriefSectors");
  secBox.innerHTML = "";
  const sectors = d.sectors || [];
  if (sectors.length) {
    const up = sectors.filter((s) => s.change_percent > 0).length;
    const down = sectors.filter((s) => s.change_percent < 0).length;
    const maxAbs = Math.max(...sectors.map((s) => Math.abs(s.change_percent)), 0.01);

    const wrap = document.createElement("div");
    wrap.className = "sec-perf";
    wrap.innerHTML =
      `<div class="sec-perf-meta"><span class="up">강세 ${up}</span><span class="down">약세 ${down}</span>
         <span class="sec-perf-src">섹터 ETF 종가 기준</span></div>` +
      sectors
        .map((s) => {
          const tone = s.change_percent > 0 ? "up" : s.change_percent < 0 ? "down" : "flat";
          const width = (Math.abs(s.change_percent) / maxAbs) * 100;
          return `<div class="sec-row ${tone}">
            <span class="sec-name">${escapeHtml(s.name)}</span>
            <span class="sec-track"><span class="sec-fill" style="width:${width.toFixed(1)}%"></span></span>
            <span class="sec-chg num">${changeWithMark(formatChangePercent(s.change_percent), tone)}</span>
          </div>`;
        })
        .join("");
    secBox.appendChild(mbriefSection("📶 섹터 성과", [wrap]));
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
            <span class="mbrief-us-chg num ${isStateBadge(c.us_change) ? "" : up ? "up" : "down"}">${changeWithMark(c.us_change, up ? "up" : "down")}</span>
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
  // 리포트가 아직 없으면(주말 등) 빈 화면 대신 안내를 띄운다.
  const morning = document.getElementById("morningBrief");
  const verdict = document.getElementById("todayVerdict");
  document.getElementById("briefingEmpty").hidden = !(morning.hidden && verdict.hidden);
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
    list.innerHTML = '<div class="empty-state"><span class="empty-icon">📉</span>기술적 분석 기능은 아직 준비 중입니다.<br>다음 단계에서 추가될 예정이에요.</div>';
    return;
  }

  for (const item of state.screening) {
    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML = `
      <div class="card-head">
        <div>
          <div class="card-title">${escapeHtml(item.name)}</div>
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
    const [summaries, morningBrief, calendar, screening, channels] = await Promise.all([
      loadJSON("summaries.json"),
      loadJSON("morning_brief.json").catch(() => ({})),
      loadJSON("calendar.json").catch(() => ({ months: [], events: [] })),
      loadJSON("screening.json"),
      loadJSON("channels.json").catch(() => []),
    ]);
    state.summaries = summaries;
    state.morningBrief = morningBrief;
    state.calendar = calendar;
    state.screening = screening;
    state.channels = channels;

    const activeCount = channels.filter((c) => !c.paused).length;
    document.getElementById("brandSub").textContent = `${activeCount}개 채널 · 자동 리포트`;

    renderCalendar();
    renderMorningBrief();
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

setupCalendarControls();
renderSkeleton();
loadAll();
