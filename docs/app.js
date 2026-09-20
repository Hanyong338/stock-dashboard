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
  screening: {},
  themes: {},
  morningBreakout: {},
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

/* ---------- 마켓 캘린더 ---------- */

const CAL_CATEGORIES = [
  { key: "major", label: "주요 이벤트" },
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

const CAL_KNOWN = new Set(CAL_CATEGORIES.map((c) => c.key));

function calEvents() {
  // 범주를 없앤 직후에는 옛 JSON 이 잠시 그대로 서빙된다. 필터 칩이 없는 범주가
  // 지울 수도 없는 채로 달력에 남지 않도록, 아는 범주만 그린다.
  return (state.calendar.events || []).filter(
    (e) => CAL_KNOWN.has(e.category) && !calState.hidden.has(e.category)
  );
}

/** 'YYYY-MM-DD' 를 로컬 자정으로 읽는다.
 *  new Date('2026-09-26') 는 UTC 자정이라 한국시간에서 9시간 밀려 비교가 틀어진다. */
function parseDay(s) {
  return new Date(s + "T00:00:00");
}

/** 달력 한 칸에는 3줄까지만 보이고 나머지는 +N 으로 접힌다.
 *  그래서 줄을 어떤 순서로 채우느냐가 곧 '무엇이 보이느냐'가 된다.
 *  지표가 10건씩 몰리는 날에 FOMC 가 접혀버리면 달력을 볼 이유가 없으므로,
 *  날짜보다 중요도를 먼저 본다. 작을수록 위쪽 줄. */
const CAL_RANK = { major: 0, holiday: 1, macro: 2, earnings: 3 };

/** 한 주(7칸) 안에서 막대가 서로 겹치지 않도록 줄(lane)을 배정한다. */
function assignLanes(events, weekStart, weekEnd) {
  const placed = [];
  const lanes = [];

  const sorted = [...events].sort((a, b) => {
    const ra = CAL_RANK[a.category] ?? 9;
    const rb = CAL_RANK[b.category] ?? 9;
    if (ra !== rb) return ra - rb; // FOMC·금통위 같은 주요 이벤트가 항상 맨 위 줄
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
    ["mbriefSectors", "업종", (d.sectors || []).length],
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
    // 뉴스 한 건을 '팩트 -> 월가 해석 -> 국내 영향' 세 층으로 보여준다.
    // comment 는 옛 리포트(한 줄 코멘트) 형식이라, 새 방송이 올라오기 전까지는 그걸 그대로 쓴다.
    const layer = (icon, label, text) =>
      text
        ? `<div class="mbrief-news-layer">
             <span class="mbrief-news-label">${icon} ${label}</span>
             <p>${inlineMd(escapeHtml(text))}</p>
           </div>`
        : "";

    const list = document.createElement("div");
    list.className = "mbrief-news";
    list.innerHTML = news
      .map(
        (n) => `<div class="mbrief-news-item">
          <div class="mbrief-news-title">${inlineMd(escapeHtml(n.title))}</div>
          ${layer("📌", "핵심 배경 및 팩트", n.fact)}
          ${layer("🔍", "월가 시각 및 시장 행간", n.street_view)}
          ${layer("💡", "국내 증시 &amp; 섹터 영향", n.korea_impact)}
          ${layer("💬", "한 줄 코멘트", n.street_view || n.korea_impact ? "" : n.comment)}
        </div>`
      )
      .join("");
    newsBox.appendChild(mbriefSection(`📰 간밤 핵심 뉴스 심층 분석 (${news.length})`, [list]));
  }

  // 업종별 성과 (실제 업종 ETF 시세 기준. 방송 발언이 아님)
  const secBox = document.getElementById("mbriefSectors");
  secBox.innerHTML = "";
  const sectors = d.sectors || [];
  if (sectors.length) {
    const up = sectors.filter((s) => s.change_percent > 0).length;
    const down = sectors.filter((s) => s.change_percent < 0).length;
    const maxAbs = Math.max(...sectors.map((s) => Math.abs(s.change_percent)), 0.01);
    const toneOf = (v) => (v > 0 ? "up" : v < 0 ? "down" : "flat");

    // 업종이 20개가 넘어가면 줄 세우기로는 눈에 안 들어온다. 색 농도로 강약을 한눈에 보여주고,
    // 오늘 제일 센 곳과 제일 약한 곳만 따로 크게 뽑아 3초 안에 결론이 잡히게 한다.
    const lead = (label, s) => {
      const tone = toneOf(s.change_percent);
      return `<div class="sec-lead-card ${tone}">
        <span class="sec-lead-label">${label}</span>
        <strong class="sec-lead-name">${escapeHtml(s.name)}</strong>
        <span class="sec-lead-chg num">${changeWithMark(formatChangePercent(s.change_percent), tone)}</span>
      </div>`;
    };

    const tiles = sectors
      .map((s) => {
        const tone = toneOf(s.change_percent);
        const i = (Math.abs(s.change_percent) / maxAbs).toFixed(3);
        return `<div class="sec-tile ${tone}" style="--i:${i}">
          <span class="sec-tile-name">${escapeHtml(s.name)}</span>
          <span class="sec-tile-chg num">${changeWithMark(formatChangePercent(s.change_percent), tone)}</span>
        </div>`;
      })
      .join("");

    const wrap = document.createElement("div");
    wrap.className = "sec-perf";
    wrap.innerHTML =
      `<div class="sec-lead">${lead("가장 강한 업종", sectors[0])}${lead("가장 약한 업종", sectors[sectors.length - 1])}</div>` +
      `<div class="sec-perf-meta"><span class="up">강세 ${up}</span><span class="down">약세 ${down}</span>
         <span class="sec-perf-src">업종 ETF 종가 기준</span></div>` +
      `<div class="sec-heat">${tiles}</div>`;
    secBox.appendChild(mbriefSection("📶 업종별 성과", [wrap]));
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
    // 주목(살 것)과 주의(피할 것)가 섞여 있으면 판단이 안 선다. 줄 자체를 갈라서
    // 주목을 먼저 두고, 주의는 아래 별도 블록으로 확실히 떼어놓는다.
    const item = (c, kind) => `<div class="mbrief-check-item ${kind}">
        <div class="mbrief-check-head">${escapeHtml(c.theme)}</div>
        <p class="mbrief-check-us">${escapeHtml(c.us)}</p>
        <p class="mbrief-check-cause">${inlineMd(escapeHtml(c.cause))}</p>
        <p class="mbrief-check-action"><span>대응</span>${inlineMd(escapeHtml(c.action))}</p>
      </div>`;

    const group = (kind, icon, title, desc, rows) => {
      if (!rows.length) return "";
      return `<div class="mbrief-checkgroup ${kind}">
        <div class="mbrief-checkgroup-head">
          <span class="mbrief-checkgroup-icon">${icon}</span>
          <span class="mbrief-checkgroup-title">${title}</span>
          <span class="mbrief-checkgroup-count num">${rows.length}</span>
          <span class="mbrief-checkgroup-desc">${desc}</span>
        </div>
        <div class="mbrief-check">${rows.map((c) => item(c, kind)).join("")}</div>
      </div>`;
    };

    const list = document.createElement("div");
    list.className = "mbrief-checkwrap";
    list.innerHTML =
      group("watch", "🔍", "주목", "기회 — 오늘 눈여겨볼 것", watch) +
      group("caution", "🚨", "주의", "리스크 — 오늘 조심할 것", caution);
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

// ──────────────────────────────────────────────────────────────────────────
// 종목 차트 — 국내 증권앱과 같은 구성 (이평선 7개 + 일목균형표 + 거래량)
// 브라우저에서 야후를 직접 못 부르므로(CORS) 배치가 저장해둔 파일을 읽는다.
// ──────────────────────────────────────────────────────────────────────────
const MA_SET = [
  { n: 5, color: "#ef4444", w: 1 },
  { n: 10, color: "#f59e0b", w: 1 },
  { n: 20, color: "#16a34a", w: 1 },
  { n: 60, color: "#0e7490", w: 1 },
  { n: 120, color: "#6366f1", w: 1 },
  { n: 240, color: "#c026d3", w: 2.4 },
  { n: 480, color: "#0d9488", w: 2.4 },
];
const chartCache = {};
let echartsReady = null;

function loadECharts() {
  // 1MB 짜리라 처음 차트를 열 때만 내려받는다. 목록만 볼 사람에게 부담을 주지 않는다.
  if (echartsReady) return echartsReady;
  echartsReady = new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = "https://cdn.jsdelivr.net/npm/echarts@5.5.1/dist/echarts.min.js";
    s.onload = () => resolve(window.echarts);
    s.onerror = () => reject(new Error("차트 라이브러리를 불러오지 못했습니다"));
    document.head.appendChild(s);
  });
  return echartsReady;
}

function movingAverage(closes, n) {
  const out = [];
  let sum = 0;
  for (let i = 0; i < closes.length; i++) {
    sum += closes[i];
    if (i >= n) sum -= closes[i - n];
    out.push(i >= n - 1 ? sum / n : null);
  }
  return out;
}

/** 일목균형표. 전환선(9) 기준선(26) 선행스팬 26칸 앞, 후행스팬 26칸 뒤. */
function ichimoku(highs, lows, closes) {
  const mid = (p) => (i) => {
    if (i < p - 1) return null;
    let h = -Infinity;
    let l = Infinity;
    for (let k = i - p + 1; k <= i; k++) {
      h = Math.max(h, highs[k]);
      l = Math.min(l, lows[k]);
    }
    return (h + l) / 2;
  };
  const n = closes.length;
  const conv = [];
  const base = [];
  const midB = mid(26);
  const midC = mid(9);
  const midD = mid(52);
  for (let i = 0; i < n; i++) {
    conv.push(midC(i));
    base.push(midB(i));
  }
  // 선행스팬은 26칸 오른쪽으로 민다. 그래서 차트가 미래 26칸까지 늘어난다.
  const spanA = new Array(n + 26).fill(null);
  const spanB = new Array(n + 26).fill(null);
  const lag = new Array(n + 26).fill(null);
  for (let i = 0; i < n; i++) {
    if (conv[i] != null && base[i] != null) spanA[i + 26] = (conv[i] + base[i]) / 2;
    const d = midD(i);
    if (d != null) spanB[i + 26] = d;
    if (i - 26 >= 0) lag[i - 26] = closes[i];
  }
  return { conv, base, spanA, spanB, lag };
}

function buildChartOption(ec, data, dark) {
  const bars = data.bars;
  const dates = bars.map((b) => b[0]);
  const opens = bars.map((b) => b[1]);
  const highs = bars.map((b) => b[2]);
  const lows = bars.map((b) => b[3]);
  const closes = bars.map((b) => b[4]);
  const vols = bars.map((b) => b[5]);

  const ich = ichimoku(highs, lows, closes);
  // 선행스팬이 26칸 앞으로 나가므로 x축도 그만큼 늘려준다 (구름이 미래로 뻗는다)
  const future = [];
  for (let i = 1; i <= 26; i++) future.push(`+${i}`);
  const axis = dates.concat(future);

  const up = "#f2465a";
  const down = "#3b82f6";
  const grid = dark ? "#262735" : "#ececf4";
  const text = dark ? "#989ab0" : "#6b6d80";

  const maValues = MA_SET.map((m) => movingAverage(closes, m.n));

  /** 보이는 구간의 캔들과 이평선이 모두 들어가도록 축 범위를 잡는다. */
  function priceRange(s, e) {
    let lo = Infinity;
    let hi = -Infinity;
    for (let i = Math.max(0, s); i <= Math.min(e, closes.length - 1); i++) {
      lo = Math.min(lo, lows[i]);
      hi = Math.max(hi, highs[i]);
      for (const arr of maValues) {
        if (arr[i] != null) {
          lo = Math.min(lo, arr[i]);
          hi = Math.max(hi, arr[i]);
        }
      }
    }
    if (!isFinite(lo) || !isFinite(hi)) return {};
    const pad = (hi - lo) * 0.06 || hi * 0.05;
    return { min: Math.max(0, Math.round(lo - pad)), max: Math.round(hi + pad) };
  }

  const maSeries = MA_SET.map((m, mi) => ({
    name: `${m.n}`,
    type: "line",
    data: maValues[mi],
    smooth: true,
    symbol: "none",
    lineStyle: { width: m.w, color: m.color },
    z: 3,
  }));

  // 5·20 골든/데드 크로스 지점에 화살표를 찍는다
  const ma5 = maValues[0];
  const ma20 = maValues[2];
  const marks = [];
  for (let i = 1; i < closes.length; i++) {
    if (ma5[i] == null || ma20[i] == null || ma5[i - 1] == null || ma20[i - 1] == null) continue;
    if (ma5[i - 1] <= ma20[i - 1] && ma5[i] > ma20[i])
      marks.push({ coord: [dates[i], lows[i]], symbol: "triangle", symbolSize: 9, itemStyle: { color: up } });
    if (ma5[i - 1] >= ma20[i - 1] && ma5[i] < ma20[i])
      marks.push({
        coord: [dates[i], highs[i]],
        symbol: "triangle",
        symbolRotate: 180,
        symbolSize: 9,
        itemStyle: { color: down },
      });
  }

  // 최근 120봉만 먼저 보여준다. 증권앱 기본 화면과 비슷한 밀도다.
  const VIEW = 120;
  const from = Math.max(0, closes.length - VIEW);
  const startPct = (from / axis.length) * 100;

  // 최저점은 반드시 '보이는 구간' 안에서 찾는다.
  // 전체에서 찾으면 몇 년 전 바닥이 잡혀 기준선이 화면 밖으로 나가고,
  // 그 선 때문에 Y축이 0까지 눌려서 캔들이 위쪽에 납작하게 깔린다.
  let lowIdx = from;
  for (let i = from; i < lows.length; i++) if (lows[i] < lows[lowIdx]) lowIdx = i;
  const lowVal = lows[lowIdx];
  const gain = (((closes[closes.length - 1] - lowVal) / lowVal) * 100).toFixed(2);

  const option = {
    backgroundColor: "transparent",
    animation: false,
    legend: {
      data: MA_SET.map((m) => `${m.n}`).concat(["전환선", "기준선", "후행스팬"]),
      top: 0,
      textStyle: { color: text, fontSize: 10 },
      itemWidth: 14,
      itemHeight: 8,
    },
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "cross" },
      backgroundColor: dark ? "#1a1b25" : "#fff",
      borderColor: grid,
      textStyle: { color: dark ? "#eef0f7" : "#12131b", fontSize: 11 },
    },
    axisPointer: { link: [{ xAxisIndex: "all" }] },
    grid: [
      { left: 8, right: 58, top: 30, height: "58%" },
      { left: 8, right: 58, top: "74%", height: "16%" },
    ],
    xAxis: [
      {
        type: "category",
        data: axis,
        boundaryGap: true,
        axisLine: { lineStyle: { color: grid } },
        axisLabel: { color: text, fontSize: 10 },
        splitLine: { show: false },
      },
      {
        type: "category",
        gridIndex: 1,
        data: axis,
        axisLine: { lineStyle: { color: grid } },
        axisLabel: { show: false },
      },
    ],
    yAxis: [
      {
        scale: true,
        position: "right",
        // 구름대를 두 시리즈로 쌓아 그리는데, 스팬이 없는 구간의 빈 값이 0으로 잡혀
        // 축이 0까지 끌려 내려간다. 그래서 축 범위는 보이는 구간의 가격/이평선으로 직접 정한다.
        ...priceRange(from, closes.length - 1),
        axisLabel: { color: text, fontSize: 10, formatter: (v) => v.toLocaleString() },
        splitLine: { lineStyle: { color: grid } },
      },
      {
        scale: true,
        gridIndex: 1,
        position: "right",
        axisLabel: { color: text, fontSize: 9, formatter: (v) => (v >= 10000 ? `${Math.round(v / 10000)}만` : v) },
        splitLine: { show: false },
      },
    ],
    dataZoom: [
      { type: "inside", xAxisIndex: [0, 1], start: startPct, end: 100 },
      { type: "slider", xAxisIndex: [0, 1], start: startPct, end: 100, height: 16, bottom: 4 },
    ],
    series: [
      // 구름대: 아래쪽 스팬을 투명하게 깔고, 두 스팬의 차이만큼을 쌓아 색을 채운다
      {
        name: "선행스팬 하단",
        type: "line",
        data: ich.spanA.map((a, i) => (a != null && ich.spanB[i] != null ? Math.min(a, ich.spanB[i]) : null)),
        stack: "cloud",
        symbol: "none",
        lineStyle: { opacity: 0 },
        areaStyle: { opacity: 0 },
        silent: true,
        z: 1,
      },
      {
        name: "구름대",
        type: "line",
        data: ich.spanA.map((a, i) =>
          a != null && ich.spanB[i] != null ? Math.abs(a - ich.spanB[i]) : null
        ),
        stack: "cloud",
        symbol: "none",
        lineStyle: { opacity: 0 },
        areaStyle: { color: dark ? "rgba(129,140,248,0.28)" : "rgba(99,102,241,0.18)" },
        silent: true,
        z: 1,
      },
      {
        name: "캔들",
        type: "candlestick",
        data: bars.map((b) => [b[1], b[4], b[3], b[2]]),
        itemStyle: {
          color: up,
          color0: down,
          borderColor: up,
          borderColor0: down,
        },
        markPoint: { data: marks, silent: true },
        markLine: {
          symbol: "none",
          silent: true,
          label: {
            formatter: `최저 ${lowVal.toLocaleString()} (${dates[lowIdx]}) +${gain}%`,
            color: text,
            fontSize: 10,
            position: "insideEndTop",
          },
          lineStyle: { color: down, type: "dashed", width: 1 },
          data: [{ yAxis: lowVal }],
        },
        z: 5,
      },
      ...maSeries,
      { name: "전환선", type: "line", data: ich.conv, symbol: "none", lineStyle: { width: 1, color: "#f97316" }, z: 2 },
      { name: "기준선", type: "line", data: ich.base, symbol: "none", lineStyle: { width: 1, color: "#10b981" }, z: 2 },
      { name: "후행스팬", type: "line", data: ich.lag, symbol: "none", lineStyle: { width: 1, color: "#a16207" }, z: 2 },
      {
        name: "거래량",
        type: "bar",
        xAxisIndex: 1,
        yAxisIndex: 1,
        data: vols.map((v, i) => ({ value: v, itemStyle: { color: closes[i] >= opens[i] ? up : down } })),
      },
    ],
  };

  return { option, priceRange, axisLength: axis.length };
}

async function openChart(code, name, host) {
  host.innerHTML = '<p class="scr-chart-loading">차트를 불러오는 중...</p>';
  try {
    const [ec, data] = await Promise.all([
      loadECharts(),
      chartCache[code] || (chartCache[code] = loadJSON(`charts/${code}.json`)),
    ]);
    host.innerHTML = "";
    const box = document.createElement("div");
    box.className = "scr-chart-canvas";
    host.appendChild(box);
    const dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
    const inst = ec.init(box, null, { renderer: "canvas" });
    const built = buildChartOption(ec, data, dark);
    inst.setOption(built.option);
    // 증권앱처럼 보이는 구간에 맞춰 세로축이 따라 움직이게 한다.
    // 고정해두면 옛 구간으로 스크롤했을 때 캔들이 화면 밖으로 나가거나 납작해진다.
    inst.on("dataZoom", () => {
      const z = inst.getOption().dataZoom[0];
      const n = built.axisLength;
      const s = Math.floor((z.start / 100) * n);
      const e = Math.ceil((z.end / 100) * n);
      inst.setOption({ yAxis: [built.priceRange(s, e), {}] });
    });
    new ResizeObserver(() => inst.resize()).observe(box);
  } catch (e) {
    delete chartCache[code];
    host.innerHTML = `<p class="scr-chart-loading">차트를 불러오지 못했습니다. (${escapeHtml(e.message)})</p>`;
  }
}

function scrCard(item, kind) {
  const tone = changeToneFromText(String(item.change_percent));
  const chg = `${item.change_percent > 0 ? "+" : ""}${Number(item.change_percent).toFixed(2)}%`;
  const flow = item.flow
    ? ["foreign", "organ", "individual"]
        .map((k, i) => {
          const v = item.flow[k];
          const label = ["외인", "기관", "개인"][i];
          const t = v > 0 ? "up" : v < 0 ? "down" : "flat";
          return `<span class="scr-flow ${t}">${label} ${v > 0 ? "+" : ""}${(v / 10000).toFixed(0)}만</span>`;
        })
        .join("")
    : "";

  const badge = `<span class="scr-badge ${kind}">유형 ${escapeHtml(item.type || "-")}</span>`;

  // 태그가 있어야 뭘 하는 회사인지 바로 안다. 종목명만으론 판단이 안 된다.
  const tags = (item.tags || []).map((t) => `<span class="scr-tag">${escapeHtml(t)}</span>`).join("");

  // 청산 규칙은 접어둔다. 종목을 훑을 때는 방해가 되고, 실제로 들어갈 때만 필요하다.
  const exits = (item.exits || []).length
    ? `<details class="scr-exit"><summary>청산·손절 규칙</summary><ul>${item.exits
        .map((x) => `<li>${escapeHtml(x)}</li>`)
        .join("")}</ul></details>`
    : "";

  return `<div class="scr-card ${kind}" data-code="${item.code}" data-name="${escapeHtml(item.name)}">
    <div class="scr-head">
      <button class="scr-name" type="button" data-chart="${item.code}">${escapeHtml(item.name)}</button>
      <span class="scr-code">${escapeHtml(item.code)}</span>
      <span class="scr-price num">${Number(item.close).toLocaleString()}</span>
      <span class="scr-chg num ${tone}">${changeWithMark(chg, tone)}</span>
    </div>
    <div class="scr-badges">${badge}${tags}<span class="scr-mkt">${escapeHtml(item.market)}</span>
      <a class="scr-ext" href="${item.url}" target="_blank" rel="noopener">네이버 ↗</a></div>
    <p class="scr-reason">${escapeHtml(item.reason)}</p>
    <div class="scr-size">${
      item.value_30m_eok != null
        ? `30분 거래대금 <b>${Number(item.value_30m_eok).toLocaleString()}억</b>
           · 거래량 전일의 <b>${Number(item.vol_vs_prev_day || 0)}%</b>
           · 시초 갭 <b>${item.open_gap > 0 ? "+" : ""}${Number(item.open_gap || 0).toFixed(1)}%</b>`
        : `거래대금 <b>${Number(item.trading_value_eok || 0).toLocaleString()}억</b>
           · 시총 <b>${Number(item.market_cap_eok || 0).toLocaleString()}억</b>`
    }</div>
    ${flow ? `<div class="scr-flows">${flow}</div>` : ""}
    ${exits}
    <div class="scr-chart" hidden></div>
  </div>`;
}

function scrSection(icon, title, desc, rows, kind, legend) {
  if (!rows.length) return "";
  // 카드 뱃지에 'A/B/C' 만 뜨면 무슨 뜻인지 알 수 없어 제목 아래에 범례를 깔아둔다.
  // 눌림목은 뱃지가 '20일선' 처럼 그 자체로 읽히므로 범례를 넣지 않는다.
  const legendBar = (legend || []).length
    ? `<ul class="scr-legend">${legend.map((t) => `<li>${escapeHtml(t)}</li>`).join("")}</ul>`
    : "";
  return `<section class="scr-group ${kind}">
    <div class="scr-group-head">
      <span class="scr-group-icon">${icon}</span>
      <span class="scr-group-title">${escapeHtml(title)}</span>
      <span class="scr-group-count num">${rows.length}</span>
    </div>
    ${legendBar}
    <p class="scr-group-desc">${escapeHtml(desc)}</p>
    <div class="scr-list">${rows.map((r) => scrCard(r, kind)).join("")}</div>
  </section>`;
}

function themeLeaders(list, updatedAt) {
  if (!list || !list.length) return "";
  // 테마는 매시간 따로 갱신된다. 종목 목록(마감 후 1회)과 시점이 달라 언제 기준인지 밝혀둔다.
  const when = updatedAt ? `<span class="scr-themes-when">${escapeHtml(formatRelativeTime(updatedAt))}</span>` : "";
  return `<section class="scr-themes">
    <div class="scr-themes-head">🔥 오늘 주도 테마${when}</div>
    <div class="scr-themes-row">${list
      .map(
        (t) => `<span class="scr-theme">
          <b>${escapeHtml(t.name)}</b>
          <em class="num up">+${Number(t.change_percent).toFixed(2)}%</em>
          <i>${t.rise}/${t.total}</i>
        </span>`
      )
      .join("")}</div>
  </section>`;
}

function renderScreening() {
  const list = document.getElementById("screeningList");
  const d = state.screening || {};
  const sections = d.sections || [];
  const total =
    sections.reduce((n, s) => n + (s.items || []).length, 0) +
    ((state.morningBreakout || {}).items || []).length;

  if (!total) {
    // 실패했을 때 '준비 중'으로만 보이면 원인을 영영 모른다. 무엇이 막혔는지 그대로 띄운다.
    const diag = d.error
      ? `<div class="scr-error"><strong>스크리닝 실패</strong><p>${escapeHtml(d.error)}</p>${
          d.probe
            ? `<ul>${Object.entries(d.probe)
                .map(([k, v]) => `<li>${escapeHtml(k)}: ${escapeHtml(String(v))}</li>`)
                .join("")}</ul>`
            : ""
        }</div>`
      : "";
    list.innerHTML =
      diag ||
      '<div class="empty-state"><span class="empty-icon">📉</span>스크리닝 결과가 아직 없습니다.<br>매일 아침 8시·저녁 7시에 전종목을 훑습니다.</div>';
    return;
  }

  // 장중 실행은 그날 일봉이 안 끝난 상태로 판정한 것이라 마감 후 결과와 달라질 수 있다.
  const intraday = d.intraday
    ? `<p class="scr-intraday">⏱ 장중 집계 — 당일 일봉이 아직 확정되지 않았습니다. 종가 기준 판정은 17시 결과를 보세요.</p>`
    : "";

  const meta = `<p class="scr-meta">${escapeHtml(d.as_of_trading_day || "")} 종가 기준 ·
    전종목 ${Number(d.universe_count || 0).toLocaleString()}개 → 체급 통과 ${Number(d.base_passed || 0).toLocaleString()}개
    ${d.dropped ? ` → 킬스위치 탈락 ${Number(d.dropped).toLocaleString()}개` : ""}
    ${d.fetch_failures ? ` · 조회 실패 ${d.fetch_failures}개` : ""}</p>`;

  // 섹션4는 09:30 장중 분봉 기준이라 1~3(마감 후 일봉)과 기준 시각이 다르다. 맨 위에 따로 둔다.
  const mb = state.morningBreakout || {};
  const morning = (mb.items || []).length
    ? `<section class="scr-group morning">
        <div class="scr-group-head">
          <span class="scr-group-icon">⚡</span>
          <span class="scr-group-title">${escapeHtml(mb.name || "모닝 브레이크아웃")}</span>
          <span class="scr-group-count num">${mb.items.length}</span>
          <span class="scr-group-when">${escapeHtml(mb.as_of || "")} 기준</span>
        </div>
        <ul class="scr-legend">${(mb.legend || []).map((t) => `<li>${escapeHtml(t)}</li>`).join("")}</ul>
        <p class="scr-group-desc">${escapeHtml(mb.desc || "")}</p>
        <div class="scr-list">${mb.items.map((r) => scrCard(r, "morning")).join("")}</div>
      </section>`
    : "";

  const icons = { CLOSING_BET: "🎯", SWING_PULLBACK: "📉", TREND_RALLY: "🚀" };
  // 테마는 themes.json 이 매시간 갱신한다. 없으면 스크리닝에 박힌 값으로 물러난다.
  const th = state.themes || {};
  list.innerHTML =
    intraday +
    meta +
    themeLeaders(th.leaders || d.theme_leaders, th.updated_at) +
    morning +
    sections
      .map((s) => scrSection(icons[s.id] || "📊", s.name, s.desc, s.items || [], "entry", s.legend))
      .join("");

  // 종목명을 누르면 그 카드 안에서 차트가 펼쳐진다. 목록을 벗어나지 않게 하려는 것.
  list.querySelectorAll("[data-chart]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const card = btn.closest(".scr-card");
      const host = card.querySelector(".scr-chart");
      if (!host.hidden) {
        host.hidden = true;
        host.innerHTML = "";
        card.classList.remove("open");
        return;
      }
      host.hidden = false;
      card.classList.add("open");
      openChart(card.dataset.code, card.dataset.name, host);
    });
  });
}

function setLastUpdated() {
  const el = document.getElementById("lastUpdated");
  const latest = state.summaries[0]?.fetched_at;
  el.textContent = latest ? `마지막 업데이트: ${formatRelativeTime(latest)}` : "데이터 없음";
}

async function loadAll() {
  try {
    const [summaries, morningBrief, calendar, screening, themes, morningBreakout, channels] = await Promise.all([
      loadJSON("summaries.json"),
      loadJSON("morning_brief.json").catch(() => ({})),
      loadJSON("calendar.json").catch(() => ({ months: [], events: [] })),
      loadJSON("screening.json").catch(() => ({})),
      loadJSON("themes.json").catch(() => ({})),
      loadJSON("morning_breakout.json").catch(() => ({})),
      loadJSON("channels.json").catch(() => []),
    ]);
    state.summaries = summaries;
    state.morningBrief = morningBrief;
    state.calendar = calendar;
    state.screening = screening;
    state.themes = themes;
    state.morningBreakout = morningBreakout;
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
