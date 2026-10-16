"use strict";
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const usd = (n, sign) => (n == null ? "—" : (sign && n > 0 ? "+" : n < 0 ? "−" : "") + "$" + Math.abs(n).toFixed(2));
const pct = (n) => (n == null ? "—" : Math.round(n * 100) + "%");
const ago = (iso) => { if (!iso) return ""; const d = (Date.now() - new Date(iso)) / 60000;
  return d < 1 ? "방금" : d < 60 ? Math.round(d) + "분 전" : d < 1440 ? Math.round(d / 60) + "시간 전" : Math.round(d / 1440) + "일 전"; };
const until = (iso) => { if (!iso) return ""; const h = (new Date(iso) - Date.now()) / 3600000;
  return h < 0 ? "마감" : h < 1 ? Math.round(h * 60) + "분 후 마감" : h < 48 ? Math.round(h) + "시간 후 마감" : Math.round(h / 24) + "일 후 마감"; };

async function api(path, body) {
  const r = await fetch(path, body === undefined ? {} : {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}
function toast(msg) { const t = $("#toast"); t.textContent = msg; t.classList.remove("hidden"); clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.add("hidden"), 3200); }
function sheet(html) { $("#sheetContent").innerHTML = html; $("#sheet").classList.remove("hidden"); }
$("#sheetClose").onclick = () => $("#sheet").classList.add("hidden");
$("#sheet").onclick = (e) => { if (e.target.id === "sheet") $("#sheet").classList.add("hidden"); };

// 탭
let currentTab = "home";
document.querySelectorAll(".bottom button").forEach((b) => b.onclick = () => {
  document.querySelectorAll(".bottom button").forEach((x) => x.classList.toggle("active", x === b));
  currentTab = b.dataset.tab;
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.id === "tab-" + currentTab));
  if (currentTab === "journal") { loadJournal(); $("#journalDot").classList.add("hidden"); }
  if (currentTab === "settings") loadSettings();
  if (currentTab === "search" && !$("#scanList").children.length) doScan();
  window.scrollTo(0, 0);
});

// 홈
let S = null, lastRetroCount = null;
function spark(curve, start) {
  const svg = $("#spark");
  const pts = [start, ...curve.map((c) => c.equity)];
  if (pts.length < 2) { svg.innerHTML = `<line x1="0" y1="30" x2="300" y2="30" stroke="var(--line)" stroke-width="2" stroke-dasharray="4 4"/>`; return; }
  const lo = Math.min(...pts), hi = Math.max(...pts), rng = hi - lo || 1;
  const xy = pts.map((v, i) => [i / (pts.length - 1) * 300, 54 - (v - lo) / rng * 48]);
  const d = xy.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" ");
  svg.innerHTML = `<path d="${d} L300 60 L0 60 Z" fill="var(--accent-soft)"/><path d="${d}" fill="none" stroke="var(--accent)" stroke-width="2.5" stroke-linejoin="round"/>`;
}
function posItem(e, settled) {
  const chip = settled ? (e.status === "won" ? `<span class="chip good">이김 ${usd(e.pnl_usd, 1)}</span>` : e.status === "lost" ? `<span class="chip bad">짐 ${usd(e.pnl_usd, 1)}</span>` : `<span class="chip gray">무효</span>`)
    : `<span class="chip">${esc(e.outcome)} @ ${e.entry_price}</span>`;
  return `<div class="item" data-pos="${esc(e.id)}"><div class="q">${esc(e.question)}</div>
    <div class="meta">${chip}<span>내 확률 ${pct(e.est_prob)}</span><span>$${e.stake_usd}</span><span>${esc(e.edge_class)}</span>
    <span>${settled ? ago(e.settled_ts) : until(e.end_date)}</span></div></div>`;
}
let POS = [];
async function loadHome() {
  try {
    S = await api("/api/state");
  } catch (e) { $("#runPill").textContent = "서버 연결 끊김"; return; }
  const sc = S.score, o = sc.overall, eq = S.status.cash + S.status.open_stake;
  $("#equity").textContent = usd(eq);
  const pnl = o.n ? o.pnl : 0;
  $("#pnl").innerHTML = `<span class="${pnl >= 0 ? "pos" : "neg"}">${usd(pnl, 1)}</span> 실현 손익 · 시작 ${usd(sc.bankroll_start)}`;
  $("#sWin").textContent = o.n ? pct(o.win_rate) : "—";
  $("#sSettled").textContent = o.n || 0;
  $("#sOpen").textContent = S.status.open_positions;
  $("#sBrier").textContent = o.n ? (o.brier_delta < 0 ? "더 정확" : "덜 정확") : "—";
  $("#sBrier").className = o.n ? (o.brier_delta < 0 ? "pos" : "neg") : "";
  spark(sc.equity_curve, sc.bankroll_start);
  const run = S.current.running;
  $("#runPill").textContent = run ? "🐹 " + (S.current.phase || "실행 중") : S.operator.real_enabled ? "실거래 ON" : "모의투자";
  $("#runPill").classList.toggle("run", run);
  $("#btnRun").disabled = run || !S.claude_available;
  $("#btnRun").textContent = run ? "실행 중…" : "▶ 사이클 실행";
  $("#btnStop").classList.toggle("hidden", !run);
  $("#claudeWarn").classList.toggle("hidden", S.claude_available);
  $("#autoLoop").checked = S.operator.auto_loop;
  $("#loopMin").value = String(S.operator.loop_minutes);
  const lc = S.last_cycle;
  let sub = `${S.cycles_run}번째 하루를 지났어요.`;
  if (lc) sub += ` 마지막: ${ago(lc.finished)} · 베팅 ${lc.new_bets?.length ?? 0}건` + (lc.summary?.note ? ` — ${lc.summary.note}` : lc.error ? ` — ⚠️ ${lc.error}` : "");
  if (S.operator.auto_loop && S.next_auto_run) sub += ` · 다음 자동 실행 ${Math.max(0, Math.round((S.next_auto_run * 1000 - Date.now()) / 60000))}분 후`;
  $("#cycleSub").textContent = sub;
  if (run || logFollow) loadLog();
}
async function loadPositions() {
  POS = await api("/api/positions");
  const open = POS.filter((e) => e.status === "open"), settled = POS.filter((e) => e.status !== "open").sort((a, b) => (b.settled_ts || "").localeCompare(a.settled_ts || "")).slice(0, 15);
  $("#openList").innerHTML = open.map((e) => posItem(e, false)).join("") || `<div class="empty">아직 보유 포지션이 없어요. 사이클을 실행해 보세요.</div>`;
  $("#settledList").innerHTML = settled.map((e) => posItem(e, true)).join("") || `<div class="empty">아직 정산된 베팅이 없어요.</div>`;
}
document.addEventListener("click", (ev) => {
  const it = ev.target.closest("[data-pos]"); if (!it) return;
  const e = POS.find((p) => p.id === it.dataset.pos); if (!e) return;
  sheet(`<div class="title lg">${esc(e.question)}</div>
    <div class="kv mt"><b>상태</b>${esc(e.status)}<br><b>선택</b>${esc(e.outcome)}<br><b>체결가 / 내 확률</b>${e.entry_price} / ${pct(e.est_prob)} (엣지 ${e.edge})<br>
    <b>금액 / 수량</b>$${e.stake_usd} / ${e.shares}주<br><b>엣지 클래스</b>${esc(e.edge_class)} · ${esc(e.category)}<br>
    <b>마감</b>${esc(e.end_date)}<br>${e.pnl_usd != null ? `<b>손익</b>${usd(e.pnl_usd, 1)}<br>` : ""}</div>
    <div class="sub mt">근거</div><div>${esc(e.rationale)}</div>
    <p><a href="https://polymarket.com/market/${esc(e.slug)}" target="_blank" rel="noopener">Polymarket 에서 보기 →</a></p>`);
});

let logFollow = false, lastLogText = "";
async function loadLog() {
  const j = await api("/api/cycle-log").catch(() => null);
  if (!j || !j.text) return;
  const el = $("#liveLog"); el.classList.remove("hidden");
  if (j.text !== lastLogText) { lastLogText = j.text; const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 30; el.textContent = j.text; if (atBottom) el.scrollTop = el.scrollHeight; }
  if (!S?.current.running && logFollow) { logFollow = false; loadPositions(); }
}
$("#btnRun").onclick = async () => {
  try { await api("/api/cycle/run", {}); logFollow = true; lastLogText = ""; toast("Phil 이 새로운 하루를 시작해요 🐹"); setTimeout(refresh, 800); } catch (e) { toast(e.message); }
};
$("#btnStop").onclick = async () => { await api("/api/cycle/stop", {}); toast("중지 요청을 보냈어요"); };
$("#btnSettle").onclick = async () => {
  try { const r = await api("/api/settle", {}); toast(`정산 ${r.settled.length}건 · 오픈 ${r.still_open}건`); refresh(); } catch (e) { toast(e.message); }
};
$("#autoLoop").onchange = async (e) => { await api("/api/operator", {auto_loop: e.target.checked}); toast(e.target.checked ? "자동 반복을 켰어요" : "자동 반복을 껐어요"); refresh(); };
$("#loopMin").onchange = async (e) => { await api("/api/operator", {loop_minutes: +e.target.value}); refresh(); };

// 탐색
async function doScan(q) {
  $("#scanList").innerHTML = `<div class="empty">불러오는 중…</div>`;
  try {
    const rows = await api("/api/scan?limit=40" + (q ? "&query=" + encodeURIComponent(q) : ""));
    $("#scanList").innerHTML = rows.map((m) => `<div class="item" data-market="${esc(m.id)}"><div class="q">${esc(m.question)}</div>
      <div class="meta">${Object.entries(m.prices).map(([k, v]) => `<span class="chip">${esc(k)} ${Math.round(v * 100)}¢</span>`).join("")}
      <span>${m.hours_left}시간 남음</span><span>24h $${(m.volume24h || 0).toLocaleString()}</span></div></div>`).join("") || `<div class="empty">결과 없음</div>`;
  } catch (e) { $("#scanList").innerHTML = `<div class="empty">오류: ${esc(e.message)}</div>`; }
}
$("#scanForm").onsubmit = (e) => { e.preventDefault(); doScan($("#scanQ").value.trim()); };
document.addEventListener("click", async (ev) => {
  const it = ev.target.closest("[data-market]"); if (!it) return;
  sheet(`<div class="empty">호가 불러오는 중…</div>`);
  try {
    const m = await api("/api/market?id=" + encodeURIComponent(it.dataset.market));
    sheet(`<div class="title lg">${esc(m.question)}</div><div class="sub">${esc(m.event || "")} · ${until(m.end_date)}</div>
      <div class="book"><b>결과</b><b>매수호가</b><b>매도호가</b>${Object.entries(m.books).map(([k, b]) => `<span>${esc(k)}</span><span>${b.bid ?? "—"}</span><span>${b.ask ?? "—"}</span>`).join("")}</div>
      <details class="mt"><summary>결의 조건</summary><div class="sub" style="white-space:pre-wrap">${esc(m.description)}</div></details>
      <p><a href="${esc(m.url)}" target="_blank" rel="noopener">Polymarket 에서 보기 →</a></p>`);
  } catch (e) { sheet(`<div class="empty">오류: ${esc(e.message)}</div>`); }
});

// 저널
async function loadJournal() {
  const [retros, strat] = await Promise.all([api("/api/retros"), api("/api/strategy")]);
  const n = retros.length, o = S?.score.overall || {};
  const winning = o.n >= 5 && o.pnl > 0 && o.brier_delta < 0;
  const labels = ["잃음", "배움", "또 배움"];
  const items = retros.slice().reverse().map((r, i) => {
    const s = r.sections["뭐가 틀렸지?"] || r.sections["규칙 고치기"] || "";
    return `<li data-retro="${esc(r.name)}"><div class="h"><span>같은 날 · ${i + 1}번째</span><span class="chip ${i ? "" : "gray"}">${labels[Math.min(i, 2)]}</span></div>
      <div class="s">✎ ${esc(r.title)}</div></li>`;
  });
  items.push(`<li class="win"><div class="box"><div class="h"><span>마지막</span><span class="chip ${winning ? "good" : ""}">${winning ? "✔ 이김" : "도전 중"}</span></div>
    <div class="s">✎ ${winning ? "드디어 — 시장보다 정확하고, 돈도 벌었어요" : `정산 ${o.n || 0}건 · 아직 시장을 이기는 중이 아니에요`}</div></div></li>`);
  $("#timeline").innerHTML = items.join("");
  $("#stratHist").innerHTML = strat.history.map((h) => `<div class="item" data-diff="${esc(h.cycle_id)}"><div class="q">✏️ ${h.paths.map(esc).join(", ")}</div><div class="meta"><span>${ago(h.ts)}</span><span>사이클 ${esc(h.cycle_id)}</span></div></div>`).join("") || `<div class="empty">아직 전략을 고친 적이 없어요.</div>`;
  $("#playbook").textContent = strat.playbook;
  const cls = S?.score.by_edge_class || {}, q = S?.score.qualified_edge_classes || [];
  $("#classTable").innerHTML = Object.entries(cls).map(([k, v]) => `<div class="item"><div class="q">${esc(k)} ${q.includes(k) ? '<span class="chip good">실거래 자격</span>' : ""}</div>
    <div class="meta"><span>${v.n}건</span><span>승률 ${pct(v.win_rate)}</span><span class="${v.pnl >= 0 ? "pos" : "neg"}">${usd(v.pnl, 1)}</span><span>brier Δ ${v.brier_delta}</span></div></div>`).join("") || `<div class="empty">정산된 베팅이 쌓이면 여기 나타나요.</div>`;
}
document.addEventListener("click", async (ev) => {
  const r = ev.target.closest("[data-retro]");
  if (r) { const j = await api("/api/retro?name=" + encodeURIComponent(r.dataset.retro)); sheet(`<pre class="doc">${esc(j.text)}</pre>`); return; }
  const d = ev.target.closest("[data-diff]");
  if (d) { const j = await api("/api/strategy-diff?cycle_id=" + encodeURIComponent(d.dataset.diff));
    sheet(j.diffs.map((x) => `<div class="title">${esc(x.path)}</div><pre class="diff">${esc(x.diff).split("\n").map((l) => l.startsWith("+") ? `<span class="a">${l}</span>` : l.startsWith("-") ? `<span class="d">${l}</span>` : l).join("\n")}</pre>`).join("")); }
});

// 설정
async function loadSettings() {
  const c = S?.real_caps;
  if (c) $("#capsText").textContent = `실거래 건당 $${c.max_stake}, 하루 $${c.daily_cap}, 동시 ${c.max_open}개, 슬리피지 ${c.max_slippage}`;
  const on = S?.operator.real_enabled;
  $("#realBadge").textContent = on ? "ON" : "OFF"; $("#realBadge").classList.toggle("on", !!on);
  $("#btnRealToggle").textContent = on ? "실거래 끄기" : "실거래 켜기";
  $("#btnRealToggle").className = "btn grow " + (on ? "danger" : "primary");
  const cycles = await api("/api/cycles");
  $("#cycleList").innerHTML = cycles.map((c) => `<div class="item" data-log="${esc(c.cycle_id)}"><div class="q">${c.ok ? "✅" : "⚠️"} ${esc(c.summary?.note || c.error || "사이클 " + c.cycle_id)}</div>
    <div class="meta"><span>${ago(c.finished)}</span><span>정산 ${c.settled ?? 0}</span><span>베팅 ${c.new_bets?.length ?? 0}</span>${c.cost_usd ? `<span>$${(+c.cost_usd).toFixed(2)}</span>` : ""}${c.reverted ? '<span class="chip bad">보호파일 복구</span>' : ""}</div></div>`).join("") || `<div class="empty">아직 실행한 사이클이 없어요.</div>`;
  $("#realDoctor").textContent = "점검 중…";
  try {
    const r = await api("/api/real");
    const d = r.doctor;
    $("#realDoctor").innerHTML = `<b>라이브러리</b>${d.library ? "✅ 설치됨" : "❌ <code>pip install -r requirements-real.txt</code>"}<br>
      <b>개인키 (.env)</b>${d.private_key_set ? "✅ 설정됨" : "❌ PHIL_PRIVATE_KEY 없음"}<br>
      <b>지갑 주소</b>${esc(d.address || "—")}<br><b>USDC 잔고</b>${d.usdc_balance != null ? "$" + d.usdc_balance : "—"}<br>
      <b>검증된 엣지</b>${d.qualified_edge_classes.length ? d.qualified_edge_classes.map(esc).join(", ") : "아직 없음 (정산 증거 부족)"}<br>
      ${d.error ? `<b>오류</b><span class="neg">${esc(d.error)}</span><br>` : ""}<b>준비 상태</b>${d.ready ? "✅ 주문 가능" : "⏳ 준비 안 됨"}`;
    $("#realList").innerHTML = r.rows.map((x) => `<div class="item"><div class="q">${esc(x.question)}</div><div class="meta"><span class="chip ${x.status === "won" ? "good" : x.status === "lost" || x.status === "rejected" || x.status === "failed" ? "bad" : ""}">${esc(x.status)}</span><span>${esc(x.outcome)} $${x.usd}</span>${x.fill_price ? `<span>@ ${x.fill_price}</span>` : ""}${x.pnl_usd != null ? `<span>${usd(x.pnl_usd, 1)}</span>` : ""}<span>${ago(x.ts)}</span>${x.error ? `<span class="neg">${esc(x.error.slice(0, 80))}</span>` : ""}</div></div>`).join("") || `<div class="empty">실거래 주문이 없어요.</div>`;
  } catch (e) { $("#realDoctor").textContent = "점검 실패: " + e.message; }
}
document.addEventListener("click", async (ev) => {
  const l = ev.target.closest("[data-log]"); if (!l) return;
  const j = await api("/api/cycle-log?id=" + encodeURIComponent(l.dataset.log)); sheet(`<pre class="log">${esc(j.text || "(로그 없음)")}</pre>`);
});
$("#btnRealToggle").onclick = async () => {
  if (S.operator.real_enabled) { await api("/api/operator", {real_enabled: false}); toast("실거래를 껐어요"); await refresh(); return loadSettings(); }
  const c = S.real_caps;
  sheet(`<div class="title lg">실거래를 켤까요?</div>
    <div class="sub">실제 USDC 로 Polymarket 에 주문해요. 정산 증거로 입증된 엣지 클래스의 새 모의 베팅만, 건당 $${c.max_stake}, 하루 최대 $${c.daily_cap} 까지 복제해요. 손실이 날 수 있어요.</div>
    <div class="sub mt">계속하려면 <b>실거래 시작</b> 을 입력하세요.</div>
    <input id="confirmTxt" class="input mt" style="width:100%" placeholder="실거래 시작">
    <button id="confirmBtn" class="btn danger mt" style="width:100%">실거래 켜기</button>`);
  $("#confirmBtn").onclick = async () => {
    try { await api("/api/operator", {real_enabled: true, confirm: $("#confirmTxt").value.trim()}); $("#sheet").classList.add("hidden"); toast("실거래를 켰어요"); await refresh(); loadSettings(); }
    catch (e) { toast(e.message); }
  };
};
$("#btnReconcile").onclick = async () => { try { const r = await api("/api/real/reconcile", {}); toast(`확인 ${r.reconciled.length}건`); loadSettings(); } catch (e) { toast(e.message); } };

async function refresh() {
  await loadHome();
  if (currentTab === "home" && (!POS.length || S?.current.running)) loadPositions();
  try { const rs = await api("/api/retros"); if (lastRetroCount != null && rs.length > lastRetroCount && currentTab !== "journal") $("#journalDot").classList.remove("hidden"); lastRetroCount = rs.length; } catch {}
}
refresh(); loadPositions();
setInterval(refresh, 4000);
