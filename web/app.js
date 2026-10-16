"use strict";
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const won = (n, sign) => (n == null ? "—" : (sign && n > 0 ? "+" : n < 0 ? "−" : "") + Math.round(Math.abs(n)).toLocaleString("ko-KR") + "원");
const pct = (n) => (n == null ? "—" : Math.round(n * 100) + "%");
const sp = (n) => (n == null ? "—" : (n > 0 ? "+" : "") + n.toFixed(2) + "%");
const ud = (n) => (n > 0 ? "up" : n < 0 ? "down" : "");
const ago = (iso) => { if (!iso) return ""; const d = (Date.now() - new Date(iso)) / 60000;
  return d < 1 ? "방금" : d < 60 ? Math.round(d) + "분 전" : d < 1440 ? Math.round(d / 60) + "시간 전" : Math.round(d / 1440) + "일 전"; };

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
let S = null, lastRetroCount = null, POS = [];
function spark(curve, start) {
  const svg = $("#spark");
  const pts = [start, ...curve.map((c) => c.equity)];
  if (pts.length < 2) { svg.innerHTML = `<line x1="0" y1="30" x2="300" y2="30" stroke="var(--line)" stroke-width="2" stroke-dasharray="4 4"/>`; return; }
  const lo = Math.min(...pts), hi = Math.max(...pts), rng = hi - lo || 1;
  const xy = pts.map((v, i) => [i / (pts.length - 1) * 300, 54 - (v - lo) / rng * 48]);
  const d = xy.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" ");
  svg.innerHTML = `<path d="${d} L300 60 L0 60 Z" fill="var(--accent-soft)"/><path d="${d}" fill="none" stroke="var(--accent)" stroke-width="2.5" stroke-linejoin="round"/>`;
}
function openItem(o) {
  return `<div class="item" data-pos="${esc(o.id)}"><div class="q">${esc(o.name)} <span class="sub">${esc(o.code)}</span></div>
    <div class="meta"><span class="chip">${o.qty}주 @ ${Math.round(o.entry).toLocaleString()}</span><span class="${ud(o.unrealized_pct)}">${sp(o.unrealized_pct)}</span>
    <span>손절 ${Math.round(o.stop).toLocaleString()}</span>${o.target ? `<span>목표 ${Math.round(o.target).toLocaleString()}</span>` : ""}<span>${o.horizon_days}일 보유</span><span>${esc(o.edge_class)}</span></div></div>`;
}
function closedItem(e) {
  return `<div class="item" data-pos="${esc(e.id)}"><div class="q">${esc(e.name)} <span class="sub">${esc(e.code)}</span></div>
    <div class="meta"><span class="chip ${e.pnl_krw > 0 ? "good" : "bad"}">${e.pnl_krw > 0 ? "이익" : "손실"} ${sp(e.ret_pct)}</span><span>${won(e.pnl_krw, 1)}</span>
    <span>${esc(e.exit_reason)}</span><span>${esc(e.edge_class)}</span><span>${ago(e.exit_ts)}</span></div></div>`;
}
async function loadHome() {
  try { S = await api("/api/state"); } catch (e) { $("#runPill").textContent = "서버 연결 끊김"; return; }
  const sc = S.score, t = sc.trades, cal = sc.calibration;
  $("#equity").textContent = won(sc.equity_krw);
  $("#pnl").innerHTML = `<span class="${ud(sc.return_pct)}">${sp(sc.return_pct)}</span> · 시작 ${won(sc.bankroll_start_krw)}` + (sc.kospi_return_pct != null ? ` · 같은 기간 코스피 <span class="${ud(sc.kospi_return_pct)}">${sp(sc.kospi_return_pct)}</span>` : "");
  $("#sKospi").textContent = sc.alpha_pct == null ? "—" : sp(sc.alpha_pct);
  $("#sKospi").className = ud(sc.alpha_pct);
  $("#sWin").textContent = t.n ? pct(t.win_rate) : "—";
  $("#sFc").textContent = sc.forecasts.total;
  $("#sBrier").textContent = cal.n ? (cal.brier_delta < 0 ? "더 정확" : "덜 정확") : "—";
  $("#sBrier").className = cal.n ? (cal.brier_delta < 0 ? "pos" : "neg") : "";
  spark(sc.equity_curve, sc.bankroll_start_krw);
  const mk = S.market || {};
  const idx = (k) => mk[k] && mk[k].price ? `${mk[k].name} ${mk[k].price.toLocaleString()} <span class="${ud(mk[k].change_pct)}">${sp(mk[k].change_pct)}</span>` : "";
  $("#marketLine").innerHTML = `${mk.can_trade_now ? "🟢 장중" : "⚪ " + esc(mk.reason || "")} · ${idx("KOSPI")} · ${idx("KOSDAQ")}`;
  const run = S.current.running;
  $("#runPill").textContent = run ? "🐹 " + (S.current.phase || "실행 중") : mk.can_trade_now ? "장중 · 모의투자" : "장 마감 · 모의투자";
  $("#runPill").classList.toggle("run", run);
  $("#btnRun").disabled = run || !S.claude_available;
  $("#btnRun").textContent = run ? "실행 중…" : mk.can_trade_now ? "▶ 사이클 실행" : "▶ 정산·회고 실행";
  $("#btnStop").classList.toggle("hidden", !run);
  $("#claudeWarn").classList.toggle("hidden", S.claude_available);
  $("#autoLoop").checked = S.operator.auto_loop;
  $("#loopMin").value = String(S.operator.loop_minutes);
  const lc = S.last_cycle;
  let sub = `${S.cycles_run}번째 하루를 지났어요.`;
  if (lc) sub += ` 마지막: ${ago(lc.finished)} · 매수 ${lc.new_bets?.length ?? 0}건 · 예측 ${lc.new_forecasts ?? 0}건` + (lc.summary?.note ? ` — ${lc.summary.note}` : lc.error ? ` — ⚠️ ${lc.error}` : "");
  if (S.operator.auto_loop) sub += S.next_auto_run ? ` · 다음 자동 실행 ${Math.max(0, Math.round((S.next_auto_run * 1000 - Date.now()) / 60000))}분 후` : " · 자동 반복: 장중과 장 마감 후(15:45)에 실행";
  $("#cycleSub").textContent = sub;
  $("#openList").innerHTML = (S.status.open || []).map(openItem).join("") || `<div class="empty">보유 종목이 없어요. 현금도 포지션이에요.</div>`;
  if (run || logFollow) loadLog();
}
async function loadPositions() {
  POS = await api("/api/positions");
  const closed = POS.filter((e) => e.status === "closed").sort((a, b) => (b.exit_ts || "").localeCompare(a.exit_ts || "")).slice(0, 15);
  $("#settledList").innerHTML = closed.map(closedItem).join("") || `<div class="empty">아직 청산된 매매가 없어요.</div>`;
}
document.addEventListener("click", (ev) => {
  const it = ev.target.closest("[data-pos]"); if (!it) return;
  const e = POS.find((p) => p.id === it.dataset.pos); if (!e) return;
  sheet(`<div class="title lg">${esc(e.name)} <span class="sub">${esc(e.code)}</span></div>
    <div class="kv mt"><b>상태</b>${e.status === "open" ? "보유 중" : esc(e.exit_reason)}<br><b>수량 / 매수가</b>${e.qty}주 / ${Math.round(e.entry_price).toLocaleString()}원<br>
    <b>매수 금액</b>${won(e.cost_krw)}<br><b>손절 / 목표</b>${Math.round(e.stop_price).toLocaleString()} / ${e.target_price ? Math.round(e.target_price).toLocaleString() : "없음"}<br>
    <b>보유 기간</b>${e.horizon_days}거래일<br><b>내 확률 / 기저율</b>${pct(e.est_prob)} / ${pct(e.base_rate)} (엣지 ${e.edge})<br><b>근거 유형</b>${esc(e.edge_class)}<br>
    ${e.pnl_krw != null ? `<b>손익</b><span class="${ud(e.pnl_krw)}">${won(e.pnl_krw, 1)} (${sp(e.ret_pct)})</span><br>` : ""}${e.calib ? `<b>만기 종가</b>${e.calib === "up" ? "올랐음" : "내렸음"} (${Math.round(e.calib_close).toLocaleString()})<br>` : ""}</div>
    <div class="sub mt">근거</div><div>${esc(e.rationale)}</div>
    <p><a href="https://m.stock.naver.com/domestic/stock/${esc(e.code)}" target="_blank" rel="noopener">네이버 증권에서 보기 →</a></p>`);
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
  const closed = !S?.market?.can_trade_now;
  try { await api("/api/cycle/run", {}); logFollow = true; lastLogText = ""; toast(closed ? "장이 닫혀 있어요. 새로 끝난 매매가 있을 때만 회고해요 (없으면 비용 0)" : "Phil 이 새로운 하루를 시작해요 🐹"); setTimeout(refresh, 800); } catch (e) { toast(e.message); }
};
$("#btnStop").onclick = async () => { await api("/api/cycle/stop", {}); toast("중지 요청을 보냈어요"); };
$("#btnSettle").onclick = async () => {
  try { const r = await api("/api/settle", {}); toast(`청산 ${r.closed.length}건 · 예측 정산 ${r.forecasts_settled.length}건`); refresh(); loadPositions(); } catch (e) { toast(e.message); }
};
$("#autoLoop").onchange = async (e) => { await api("/api/operator", {auto_loop: e.target.checked}); toast(e.target.checked ? "자동 반복을 켰어요 (장중 + 장 마감 후)" : "자동 반복을 껐어요"); refresh(); };
$("#loopMin").onchange = async (e) => { await api("/api/operator", {loop_minutes: +e.target.value}); refresh(); };

// 탐색
let scanSort = "value";
async function doScan() {
  const q = $("#scanQ").value.trim();
  $("#scanList").innerHTML = `<div class="empty">불러오는 중…</div>`;
  try {
    const rows = await api(`/api/scan?limit=50&sort=${scanSort}` + (q ? "&query=" + encodeURIComponent(q) : ""));
    $("#scanList").innerHTML = rows.map((m) => `<div class="item" data-stock="${esc(m.code)}"><div class="q">${esc(m.name)} <span class="sub">${esc(m.code)} · ${esc(m.market)}</span></div>
      <div class="meta"><span class="chip">${Math.round(m.price).toLocaleString()}원</span><span class="${ud(m.change_pct)}">${sp(m.change_pct)}</span>
      <span>거래대금 ${Math.round(m.value_mil / 100).toLocaleString()}억</span><span>${esc(m.market)} ${m.rank}위</span></div></div>`).join("") || `<div class="empty">결과 없음</div>`;
  } catch (e) { $("#scanList").innerHTML = `<div class="empty">오류: ${esc(e.message)}</div>`; }
}
$("#scanForm").onsubmit = (e) => { e.preventDefault(); doScan(); };
document.querySelectorAll("#sortChips button").forEach((b) => b.onclick = () => {
  scanSort = b.dataset.sort; document.querySelectorAll("#sortChips button").forEach((x) => x.classList.toggle("on", x === b)); doScan();
});
document.addEventListener("click", async (ev) => {
  const it = ev.target.closest("[data-stock]"); if (!it) return;
  sheet(`<div class="empty">불러오는 중…</div>`);
  try {
    const d = await api("/api/stock?code=" + encodeURIComponent(it.dataset.stock));
    const r = d.returns_pct, br = d.base_rate_up;
    sheet(`<div class="title lg">${esc(d.name)} <span class="sub">${esc(d.code)} · ${esc(d.exchange)}</span></div>
      <div class="big">${Math.round(d.price).toLocaleString()}원 <span class="${ud(d.change_pct)}" style="font-size:16px">${sp(d.change_pct)}</span></div>
      <div class="grid2"><span>5일</span><span class="${ud(r["5d"])}">${sp(r["5d"])}</span><span>20일</span><span class="${ud(r["20d"])}">${sp(r["20d"])}</span>
      <span>60일</span><span class="${ud(r["60d"])}">${sp(r["60d"])}</span><span>1년</span><span class="${ud(r["250d"])}">${sp(r["250d"])}</span>
      <span>거래량 (20일 평균 대비)</span><span>${d.volume_vs_20d_avg ?? "—"}배</span>
      ${Object.entries(d.info || {}).map(([k, v]) => `<span>${esc(k)}</span><span>${esc(v)}</span>`).join("")}</div>
      <div class="sub mt">기저율: 최근 1년 중 N거래일 뒤 올랐던 비율 (Phil 은 이걸 넘어야 매수할 수 있어요)</div>
      <div class="grid2">${Object.entries(br).map(([k, v]) => `<span>${k.replace("d", "일")} 뒤</span><span>${pct(v)}</span>`).join("")}</div>
      <div class="title mt">최근 뉴스</div><div class="list news">${(d.news || []).map((n) => `<div class="item"><a href="${esc(n.url)}" target="_blank" rel="noopener"><div class="q">${esc(n.title)}</div><div class="meta"><span>${esc(n.office)}</span><span>${esc(String(n.datetime || "").replace(/^(\d{4})(\d{2})(\d{2})(\d{2})(\d{2}).*/, "$2/$3 $4:$5"))}</span></div></a></div>`).join("") || `<div class="empty">뉴스 없음</div>`}</div>
      <p><a href="${esc(d.url)}" target="_blank" rel="noopener">네이버 증권에서 보기 →</a></p>`);
  } catch (e) { sheet(`<div class="empty">오류: ${esc(e.message)}</div>`); }
});

// 저널
async function loadJournal() {
  const [retros, strat] = await Promise.all([api("/api/retros"), api("/api/strategy")]);
  const sc = S?.score || {}, t = sc.trades || {}, cal = sc.calibration || {};
  const winning = t.n >= 10 && (sc.alpha_pct ?? -1) > 0 && cal.n >= 30 && cal.brier_delta < 0;
  const labels = ["잃음", "배움", "또 배움"];
  const items = retros.slice().reverse().map((r, i) => `<li data-retro="${esc(r.name)}"><div class="h"><span>같은 날 · ${i + 1}번째</span><span class="chip ${i ? "" : "gray"}">${labels[Math.min(i, 2)]}</span></div>
      <div class="s">✎ ${esc(r.title)}</div></li>`);
  items.push(`<li class="win"><div class="box"><div class="h"><span>마지막</span><span class="chip ${winning ? "good" : ""}">${winning ? "✔ 이김" : "도전 중"}</span></div>
    <div class="s">✎ ${winning ? "드디어 — 코스피를 이기고, 기저율보다 정확해요" : `청산 ${t.n || 0}건 · 예측 정산 ${cal.n || 0}건 · 아직 증명 중이에요`}</div></div></li>`);
  $("#timeline").innerHTML = items.join("");
  $("#stratHist").innerHTML = strat.history.map((h) => `<div class="item" data-diff="${esc(h.cycle_id)}"><div class="q">✏️ ${h.paths.map(esc).join(", ")}</div><div class="meta"><span>${ago(h.ts)}</span><span>사이클 ${esc(h.cycle_id)}</span></div></div>`).join("") || `<div class="empty">아직 전략을 고친 적이 없어요.</div>`;
  $("#playbook").textContent = strat.playbook;
  const byCal = sc.calibration_by_edge_class || {}, banned = sc.banned_edge_classes || {};
  $("#fcSub").textContent = `예측 ${sc.forecasts?.total || 0}건 기록 · 정확도 측정 ${cal.n || 0}건` + (cal.n ? ` · 전체적으로 기저율보다 ${cal.brier_delta < 0 ? "더 정확" : "덜 정확"} (Δ ${cal.brier_delta}, 방향 적중 ${pct(cal.hit_rate)})` : "") + ". 기저율보다 못한 근거 유형은 엔진이 매수를 막아요.";
  const keys = [...new Set([...Object.keys(byCal), ...Object.keys(banned)])].sort((a, b) => (byCal[b]?.n || 0) - (byCal[a]?.n || 0));
  $("#catTable").innerHTML = keys.map((k) => { const c = byCal[k], b = banned[k];
    return `<div class="item"><div class="q">${esc(k)} ${b ? '<span class="chip bad">매수 금지</span>' : c && c.n >= 10 && c.brier_delta < 0 ? '<span class="chip good">기저율보다 정확</span>' : ""}</div>
    <div class="meta">${c ? `<span>${c.n}건</span><span class="${c.brier_delta < 0 ? "pos" : "neg"}">Δ ${c.brier_delta}</span><span>적중 ${pct(c.hit_rate)}</span>` : ""}${b ? `<span>${esc(b)}</span>` : ""}</div></div>`; }).join("") || `<div class="empty">장중에 사이클을 돌리면 예측이 쌓여요.</div>`;
  const cls = sc.by_edge_class || {}, v = sc.verified_edge_classes || [];
  $("#classTable").innerHTML = Object.entries(cls).map(([k, s]) => `<div class="item"><div class="q">${esc(k)} ${v.includes(k) ? '<span class="chip good">검증된 엣지</span>' : ""}</div>
    <div class="meta"><span>${s.n}건</span><span>승률 ${pct(s.win_rate)}</span><span class="${ud(s.pnl_krw)}">${won(s.pnl_krw, 1)}</span><span>평균 ${sp(s.avg_ret_pct)}</span></div></div>`).join("") || `<div class="empty">청산된 매매가 쌓이면 여기 나타나요.</div>`;
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
  $("#policyRules").textContent = "엔진 규칙: " + (S?.policy || []).join(" · ");
  const cycles = await api("/api/cycles");
  $("#cycleList").innerHTML = cycles.map((c) => `<div class="item" data-log="${esc(c.cycle_id)}"><div class="q">${c.ok ? (c.mode ? "✅" : "😴") : "⚠️"} ${esc(c.summary?.note || c.error || "사이클 " + c.cycle_id)}</div>
    <div class="meta"><span>${ago(c.finished)}</span>${c.mode ? `<span>${c.mode === "FULL" ? "장중" : "회고"}</span>` : ""}<span>청산 ${c.settled ?? 0}</span><span>매수 ${c.new_bets?.length ?? 0}</span><span>예측 ${c.new_forecasts ?? 0}</span>${c.cost_usd ? `<span>$${(+c.cost_usd).toFixed(2)}</span>` : ""}${c.reverted ? '<span class="chip bad">보호파일 복구</span>' : ""}</div></div>`).join("") || `<div class="empty">아직 실행한 사이클이 없어요.</div>`;
}
document.addEventListener("click", async (ev) => {
  const l = ev.target.closest("[data-log]"); if (!l) return;
  const j = await api("/api/cycle-log?id=" + encodeURIComponent(l.dataset.log)); sheet(`<pre class="log">${esc(j.text || "(로그 없음)")}</pre>`);
});

async function refresh() {
  await loadHome();
  if (currentTab === "home" && S?.current.running) loadPositions();
  try { const rs = await api("/api/retros"); if (lastRetroCount != null && rs.length > lastRetroCount && currentTab !== "journal") $("#journalDot").classList.remove("hidden"); lastRetroCount = rs.length; } catch {}
}
refresh().then(loadPositions);
setInterval(refresh, 5000);
