/* Ledger frontend — plain JS, no build step. */
"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];

const state = {
  meId: null,
  users: [],          // [{id, display_name}]
  tab: "dashboard",
  month: new Date().toISOString().slice(0, 7),
  editingTxn: null,
  editingBill: null,
  payingBill: null,
  contribGoal: null,
  openLogs: new Set(),
};

const fmt = (n) =>
  "$" + Number(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

const todayISO = () => {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
};

function userById(id) {
  return state.users.find((u) => u.id === id) || { display_name: "?" };
}
function userColor(id) {
  const idx = state.users.findIndex((u) => u.id === id);
  return idx === 0 ? "var(--p1)" : "var(--p2)";
}
function esc(s) {
  const div = document.createElement("div");
  div.textContent = s == null ? "" : String(s);
  return div.innerHTML;
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (res.status === 401) {
    showAuth();
    throw new Error("authentication required");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `request failed (${res.status})`);
  return data;
}

/* ================= auth ================= */

function showAuth(setupRequired = false) {
  $("#view-app").classList.add("hidden");
  $("#view-auth").classList.remove("hidden");
  $("#form-setup").classList.toggle("hidden", !setupRequired);
  $("#form-login").classList.toggle("hidden", setupRequired);
  $("#auth-sub").textContent = setupRequired
    ? "First run — set up your two accounts."
    : "Household finance for two.";
}

async function showApp() {
  const me = await api("/api/me");
  state.meId = me.user_id;
  state.users = me.users;
  $("#view-auth").classList.add("hidden");
  $("#view-app").classList.remove("hidden");
  buildNav();
  await loadCategories();
  setTab(state.tab);
}

async function boot() {
  try {
    const s = await api("/api/status");
    if (s.setup_required) return showAuth(true);
    if (!s.logged_in) return showAuth(false);
    await showApp();
  } catch (e) {
    showAuth(false);
  }
}

$("#form-login").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.target;
  $("#login-error").textContent = "";
  try {
    await api("/api/login", {
      method: "POST",
      body: { username: f.username.value, password: f.password.value },
    });
    f.reset();
    await showApp();
  } catch (e) {
    $("#login-error").textContent = e.message;
  }
});

$("#form-setup").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.target;
  $("#setup-error").textContent = "";
  try {
    await api("/api/setup", {
      method: "POST",
      body: {
        users: [
          { display_name: f.d0.value, username: f.u0.value, password: f.p0.value },
          { display_name: f.d1.value, username: f.u1.value, password: f.p1.value },
        ],
      },
    });
    showAuth(false);
    $("#auth-sub").textContent = "Accounts created — sign in.";
  } catch (e) {
    $("#setup-error").textContent = e.message;
  }
});

$("#btn-logout").addEventListener("click", async () => {
  await api("/api/logout", { method: "POST" });
  showAuth(false);
});

/* ================= navigation ================= */

const TABS = [
  ["dashboard", "Home"],
  ["activity", "Activity"],
  ["bills", "Bills"],
  ["goals", "Goals"],
];

function buildNav() {
  for (const holder of [$("#topnav"), $("#tabbar")]) {
    holder.innerHTML = "";
    for (const [key, label] of TABS) {
      const b = document.createElement("button");
      b.textContent = label;
      b.dataset.tab = key;
      b.addEventListener("click", () => setTab(key));
      holder.appendChild(b);
    }
  }
}

function setTab(tab) {
  state.tab = tab;
  $$("#topnav button, #tabbar button").forEach((b) =>
    b.classList.toggle("on", b.dataset.tab === tab));
  render();
}

async function render() {
  const main = $("#main");
  try {
    if (state.tab === "dashboard") main.innerHTML = await renderDashboard();
    if (state.tab === "activity") main.innerHTML = await renderActivity();
    if (state.tab === "bills") main.innerHTML = await renderBills();
    if (state.tab === "goals") main.innerHTML = await renderGoals();
    wireMain();
  } catch (e) {
    if (e.message !== "authentication required")
      main.innerHTML = `<p class="empty">${esc(e.message)}</p>`;
  }
}

/* ================= dashboard ================= */

function beamHTML(bal) {
  const [u1, u2] = state.users;
  let fill = "";
  if (!bal.settled) {
    const owedIdx = state.users.findIndex((u) => u.id === bal.owed.id);
    const color = owedIdx === 0 ? "var(--p1)" : "var(--p2)";
    // tilt: cap visual width at 50% of the beam, scaled by amount (full at $200+)
    const w = Math.min(50, 10 + (bal.amount / 200) * 40);
    const side = owedIdx === 0 ? "right: 50%;" : "left: 50%;";
    fill = `<div class="beam-fill" style="${side} width:${w}%; background:${color}"></div>`;
  }
  const msg = bal.settled
    ? `<p class="beam-msg">All settled up</p>
       <p class="beam-sub">No one owes anything on shared expenses</p>`
    : `<p class="beam-msg">${esc(bal.owes.name)} owes ${esc(bal.owed.name)}
         <span class="amount">${fmt(bal.amount)}</span></p>
       <p class="beam-sub">across all shared expenses</p>`;
  const settleBtn = bal.settled
    ? ""
    : `<button class="btn small" id="btn-settle" type="button">Settle up</button>`;
  return `
    <div class="card beam-card">
      <p class="eyebrow">Between you two</p>
      ${msg}
      <div class="beam"><div class="beam-center"></div>${fill}</div>
      <div class="beam-names">
        <span class="${u1.id === state.meId ? "me" : ""}">
          <span class="dot" style="--pcolor: var(--p1)"></span>${esc(u1.display_name)}</span>
        <span class="${u2.id === state.meId ? "me" : ""}">
          <span class="dot" style="--pcolor: var(--p2)"></span>${esc(u2.display_name)}</span>
      </div>
      ${settleBtn}
    </div>`;
}

async function renderDashboard() {
  const d = await api("/api/dashboard");
  window._dash = d;
  const maxCat = Math.max(1, ...d.by_category.map((c) => c.amount));
  const cats = d.by_category.length
    ? d.by_category.map((c) => `
        <div class="cat-row">
          <span class="cat-name">${esc(c.category)}</span>
          <span class="cat-bar"><i style="width:${(c.amount / maxCat) * 100}%"></i></span>
          <span class="amt amount">${fmt(c.amount)}</span>
        </div>`).join("")
    : `<p class="empty">Nothing spent yet this month.</p>`;

  const today = new Date().getDate();
  const bills = d.unpaid_bills.length
    ? `<ul class="list">${d.unpaid_bills.map((b) => `
        <li>
          <div class="grow">
            <div class="title">${esc(b.name)}</div>
            <div class="sub">due the ${ord(b.due_day)}</div>
          </div>
          <span class="badge ${b.due_day < today ? "overdue" : "due"}">
            ${b.due_day < today ? "overdue" : "upcoming"}</span>
          <span class="amt amount">${fmt(b.amount)}</span>
        </li>`).join("")}</ul>`
    : `<p class="empty">All bills paid this month 🎉</p>`;

  const goals = d.goals.length
    ? d.goals.map((g) => `
        <div style="margin-bottom:12px">
          <div class="goal-head"><h3>${esc(g.name)}</h3>
            <span class="amt amount">${fmt(g.saved)} / ${fmt(g.target)}</span></div>
          <div class="goal-bar"><i style="width:${g.progress * 100}%"></i></div>
        </div>`).join("")
    : `<p class="empty">No goals yet — add one in the Goals tab.</p>`;

  const recent = d.recent.length
    ? `<ul class="list">${d.recent.map(txnRow).join("")}</ul>`
    : `<p class="empty">No transactions yet. Tap + to add the first one.</p>`;

  return `
    ${beamHTML(d.balance)}
    <div class="card">
      <p class="eyebrow">Spent in ${monthName(d.month)}</p>
      <p class="stat-big">${fmt(d.month_total)}</p>
      <div style="margin-top:12px">${cats}</div>
    </div>
    <div class="card"><p class="eyebrow">Unpaid bills</p>${bills}</div>
    <div class="card"><p class="eyebrow">Goals</p>${goals}</div>
    <div class="card"><p class="eyebrow">Recent</p>${recent}</div>`;
}

/* ================= activity ================= */

function txnRow(t) {
  const payer = userById(t.paid_by);
  const shared = t.is_shared
    ? t.payer_share_pct === 50 ? "shared 50/50" : `shared · payer ${t.payer_share_pct}%`
    : "personal";
  const src = t.source === "manual" ? "" :
    ` · <span class="badge">${esc(t.source)}</span>`;
  return `
    <li class="tap" data-txn="${t.id}">
      <div class="grow">
        <div class="title">${esc(t.description)}</div>
        <div class="sub">
          <span class="dot" style="--pcolor:${userColor(t.paid_by)}"></span>${esc(payer.display_name)}
          · ${esc(t.category)} · ${shared}${src}
        </div>
      </div>
      <div style="text-align:right">
        <div class="amt amount">${fmt(t.amount)}</div>
        <div class="sub">${t.date.slice(5)}</div>
      </div>
    </li>`;
}

async function renderActivity() {
  const txns = await api(`/api/transactions?month=${state.month}`);
  window._txns = txns;
  const list = txns.length
    ? `<ul class="list">${txns.map(txnRow).join("")}</ul>`
    : `<p class="empty">No transactions in ${monthName(state.month)}.</p>`;
  return `
    <div class="monthbar">
      <button id="month-prev" aria-label="Previous month">‹</button>
      <b>${monthName(state.month)}</b>
      <button id="month-next" aria-label="Next month">›</button>
    </div>
    <div class="card">${list}</div>`;
}

/* ================= bills ================= */

async function renderBills() {
  const bills = await api("/api/bills");
  window._bills = bills;
  const rows = bills.length
    ? `<ul class="list">${bills.map((b) => `
        <li>
          <div class="grow tap" data-bill-edit="${b.id}">
            <div class="title">${esc(b.name)}</div>
            <div class="sub">${esc(b.category)} · due the ${ord(b.due_day)}</div>
          </div>
          <span class="amt amount">${fmt(b.amount)}</span>
          ${b.paid_this_period
            ? `<span class="badge paid">paid</span>
               <button class="btn small ghost" data-bill-unpay="${b.id}">Undo</button>`
            : `<button class="btn small" data-bill-pay="${b.id}">Mark paid</button>`}
        </li>`).join("")}</ul>`
    : `<p class="empty">No recurring bills yet.</p>`;
  return `
    <div class="section-head">
      <p class="eyebrow" style="margin:0">Bills — ${monthName(state.month = new Date().toISOString().slice(0,7)) }</p>
      <button class="btn small" id="btn-add-bill">Add bill</button>
    </div>
    <div class="card">${rows}</div>`;
}

/* ================= goals ================= */

async function renderGoals() {
  const goals = await api("/api/goals");
  window._goals = goals;
  const cards = await Promise.all(goals.map(async (g) => {
    let log = "";
    if (state.openLogs.has(g.id)) {
      const rows = await api(`/api/goals/${g.id}/contributions`);
      log = `<div class="contrib-log"><ul class="list">${
        rows.length ? rows.map((c) => `
          <li>
            <div class="grow">
              <div class="title">${esc(c.by)}${c.note ? ` — <span class="sub">${esc(c.note)}</span>` : ""}</div>
              <div class="sub">${c.date}</div>
            </div>
            <span class="amt amount">${c.amount < 0 ? "−" : "+"}${fmt(Math.abs(c.amount))}</span>
          </li>`).join("") : `<li><div class="grow sub">No contributions yet.</div></li>`
      }</ul></div>`;
    }
    const eta = g.target_date ? ` · by ${g.target_date}` : "";
    return `
      <div class="card" data-goal-card="${g.id}">
        <div class="goal-head">
          <h3>${esc(g.name)}</h3>
          <button class="btn small" data-goal-add="${g.id}">Add</button>
        </div>
        <div class="goal-bar"><i style="width:${g.progress * 100}%"></i></div>
        <div class="goal-meta">
          <span class="amount">${fmt(g.saved)} of ${fmt(g.target)}${eta}</span>
          <span>${Math.round(g.progress * 100)}%</span>
        </div>
        <div class="goal-meta" style="margin-top:10px">
          <button class="btn small ghost" data-goal-log="${g.id}">
            ${state.openLogs.has(g.id) ? "Hide log" : "Show log"}</button>
          <button class="btn small ghost" data-goal-del="${g.id}">Delete</button>
        </div>
        ${log}
      </div>`;
  }));
  return `
    <div class="section-head">
      <p class="eyebrow" style="margin:0">Savings goals</p>
      <button class="btn small" id="btn-add-goal">New goal</button>
    </div>
    ${cards.join("") || `<p class="empty">No goals yet — create your first.</p>`}`;
}

/* ================= wiring ================= */

function ord(n) {
  const s = ["th", "st", "nd", "rd"], v = n % 100;
  return n + (s[(v - 20) % 10] || s[v] || s[0]);
}
function monthName(ym) {
  const [y, m] = ym.split("-").map(Number);
  return new Date(y, m - 1, 1).toLocaleDateString("en-US", { month: "long", year: "numeric" });
}

function wireMain() {
  $("#btn-settle")?.addEventListener("click", openSettle);
  $("#month-prev")?.addEventListener("click", () => shiftMonth(-1));
  $("#month-next")?.addEventListener("click", () => shiftMonth(1));
  $("#btn-add-bill")?.addEventListener("click", () => openBillDialog(null));
  $("#btn-add-goal")?.addEventListener("click", openGoalDialog);
  $$("[data-txn]").forEach((el) =>
    el.addEventListener("click", () => openTxnDialog(
      (window._txns || window._dash.recent).find((t) => t.id === +el.dataset.txn))));
  $$("[data-bill-pay]").forEach((el) =>
    el.addEventListener("click", () => openPayDialog(+el.dataset.billPay)));
  $$("[data-bill-unpay]").forEach((el) =>
    el.addEventListener("click", () => unpayBill(+el.dataset.billUnpay)));
  $$("[data-bill-edit]").forEach((el) =>
    el.addEventListener("click", () =>
      openBillDialog(window._bills.find((b) => b.id === +el.dataset.billEdit))));
  $$("[data-goal-add]").forEach((el) =>
    el.addEventListener("click", () => openContribDialog(+el.dataset.goalAdd)));
  $$("[data-goal-log]").forEach((el) =>
    el.addEventListener("click", () => {
      const id = +el.dataset.goalLog;
      state.openLogs.has(id) ? state.openLogs.delete(id) : state.openLogs.add(id);
      render();
    }));
  $$("[data-goal-del]").forEach((el) =>
    el.addEventListener("click", async () => {
      if (!confirm("Delete this goal and its contribution log?")) return;
      await api(`/api/goals/${el.dataset.goalDel}`, { method: "DELETE" });
      render();
    }));
}

function shiftMonth(delta) {
  const [y, m] = state.month.split("-").map(Number);
  const d = new Date(y, m - 1 + delta, 1);
  state.month = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
  render();
}

async function loadCategories() {
  const cats = await api("/api/categories");
  $("#category-list").innerHTML = cats.map((c) => `<option value="${esc(c)}">`).join("");
}

/* ---------- segmented paid-by control ---------- */
function buildSeg(holder, selectedId, onPick) {
  holder.innerHTML = "";
  state.users.forEach((u, idx) => {
    const b = document.createElement("button");
    b.type = "button";
    b.style.setProperty("--pcolor", idx === 0 ? "var(--p1)" : "var(--p2)");
    b.innerHTML = `<span class="dot"></span>${esc(u.display_name)}`;
    b.classList.toggle("on", u.id === selectedId);
    b.addEventListener("click", () => {
      holder.dataset.value = u.id;
      $$("button", holder).forEach((x) => x.classList.remove("on"));
      b.classList.add("on");
      if (onPick) onPick(u.id);
    });
    holder.appendChild(b);
  });
  holder.dataset.value = selectedId;
}

/* ---------- transaction dialog ---------- */
const dlgTxn = $("#dlg-txn");
const formTxn = $("#form-txn");

function updateSplitHint() {
  const pct = +formTxn.payer_share_pct.value;
  const payerId = +$("#txn-paidby").dataset.value;
  const payer = userById(payerId);
  const other = state.users.find((u) => u.id !== payerId) || { display_name: "Partner" };
  $("#split-readout").textContent = `${pct}%`;
  $("#split-hint").textContent =
    `${payer.display_name} covers ${pct}% · ${other.display_name} covers ${100 - pct}%`;
}

function openTxnDialog(txn) {
  state.editingTxn = txn || null;
  $("#txn-title").textContent = txn ? "Edit expense" : "Add expense";
  $("#btn-txn-delete").classList.toggle("hidden", !txn);
  $("#txn-error").textContent = "";
  formTxn.date.value = txn ? txn.date : todayISO();
  formTxn.amount.value = txn ? txn.amount : "";
  formTxn.description.value = txn ? txn.description : "";
  formTxn.category.value = txn ? txn.category : "";
  formTxn.is_shared.checked = txn ? txn.is_shared : true;
  formTxn.payer_share_pct.value = txn ? txn.payer_share_pct : 50;
  buildSeg($("#txn-paidby"), txn ? txn.paid_by : state.meId, updateSplitHint);
  $("#txn-split").classList.toggle("hidden", !formTxn.is_shared.checked);
  updateSplitHint();
  dlgTxn.showModal();
}

formTxn.is_shared.addEventListener("change", () =>
  $("#txn-split").classList.toggle("hidden", !formTxn.is_shared.checked));
formTxn.payer_share_pct.addEventListener("input", updateSplitHint);

formTxn.addEventListener("submit", async (ev) => {
  if (ev.submitter && ev.submitter.value === "cancel") return;
  ev.preventDefault();
  $("#txn-error").textContent = "";
  const body = {
    date: formTxn.date.value,
    amount: formTxn.amount.value,
    description: formTxn.description.value,
    category: formTxn.category.value || "Other",
    paid_by: +$("#txn-paidby").dataset.value,
    is_shared: formTxn.is_shared.checked,
    payer_share_pct: +formTxn.payer_share_pct.value,
  };
  try {
    if (state.editingTxn) {
      await api(`/api/transactions/${state.editingTxn.id}`, { method: "PUT", body });
    } else {
      await api("/api/transactions", { method: "POST", body });
    }
    dlgTxn.close();
    render();
  } catch (e) {
    $("#txn-error").textContent = e.message;
  }
});

$("#btn-txn-delete").addEventListener("click", async () => {
  if (!state.editingTxn || !confirm("Delete this transaction?")) return;
  await api(`/api/transactions/${state.editingTxn.id}`, { method: "DELETE" });
  dlgTxn.close();
  render();
});

$("#fab").addEventListener("click", () => openTxnDialog(null));

/* ---------- bill dialogs ---------- */
const dlgBill = $("#dlg-bill");
const formBill = $("#form-bill");

function openBillDialog(bill) {
  state.editingBill = bill || null;
  $("#bill-title").textContent = bill ? "Edit bill" : "Add bill";
  $("#btn-bill-delete").classList.toggle("hidden", !bill);
  $("#bill-error").textContent = "";
  formBill.name.value = bill ? bill.name : "";
  formBill.amount.value = bill ? bill.amount : "";
  formBill.due_day.value = bill ? bill.due_day : "";
  formBill.category.value = bill ? bill.category : "";
  dlgBill.showModal();
}

formBill.addEventListener("submit", async (ev) => {
  if (ev.submitter && ev.submitter.value === "cancel") return;
  ev.preventDefault();
  const body = {
    name: formBill.name.value,
    amount: formBill.amount.value,
    due_day: +formBill.due_day.value,
    category: formBill.category.value || "Bills",
  };
  try {
    if (state.editingBill) {
      await api(`/api/bills/${state.editingBill.id}`, { method: "PUT", body });
    } else {
      await api("/api/bills", { method: "POST", body });
    }
    dlgBill.close();
    render();
  } catch (e) {
    $("#bill-error").textContent = e.message;
  }
});

$("#btn-bill-delete").addEventListener("click", async () => {
  if (!state.editingBill || !confirm("Remove this bill? Past payments stay in Activity.")) return;
  await api(`/api/bills/${state.editingBill.id}`, { method: "DELETE" });
  dlgBill.close();
  render();
});

const dlgPay = $("#dlg-pay");
const formPay = $("#form-pay");

function openPayDialog(billId) {
  const bill = window._bills.find((b) => b.id === billId);
  state.payingBill = bill;
  $("#pay-error").textContent = "";
  $("#pay-summary").textContent =
    `${bill.name} — ${fmt(bill.amount)} for ${monthName(bill.period)}. This also logs a transaction.`;
  formPay.is_shared.checked = true;
  buildSeg($("#pay-paidby"), state.meId);
  dlgPay.showModal();
}

formPay.addEventListener("submit", async (ev) => {
  if (ev.submitter && ev.submitter.value === "cancel") return;
  ev.preventDefault();
  try {
    await api(`/api/bills/${state.payingBill.id}/pay`, {
      method: "POST",
      body: {
        paid_by: +$("#pay-paidby").dataset.value,
        is_shared: formPay.is_shared.checked,
        payer_share_pct: 50,
      },
    });
    dlgPay.close();
    render();
  } catch (e) {
    $("#pay-error").textContent = e.message;
  }
});

async function unpayBill(billId) {
  if (!confirm("Undo this payment? The logged transaction is removed too.")) return;
  await api(`/api/bills/${billId}/pay`, { method: "DELETE" });
  render();
}

/* ---------- goal dialogs ---------- */
const dlgGoal = $("#dlg-goal");
const formGoal = $("#form-goal");

function openGoalDialog() {
  $("#goal-error").textContent = "";
  formGoal.reset();
  dlgGoal.showModal();
}

formGoal.addEventListener("submit", async (ev) => {
  if (ev.submitter && ev.submitter.value === "cancel") return;
  ev.preventDefault();
  try {
    await api("/api/goals", {
      method: "POST",
      body: {
        name: formGoal.name.value,
        target: formGoal.target.value,
        target_date: formGoal.target_date.value || null,
      },
    });
    dlgGoal.close();
    render();
  } catch (e) {
    $("#goal-error").textContent = e.message;
  }
});

const dlgContrib = $("#dlg-contrib");
const formContrib = $("#form-contrib");

function openContribDialog(goalId) {
  state.contribGoal = goalId;
  const g = window._goals.find((x) => x.id === goalId);
  $("#contrib-title").textContent = `Add to “${g.name}”`;
  $("#contrib-error").textContent = "";
  formContrib.reset();
  dlgContrib.showModal();
}

formContrib.addEventListener("submit", async (ev) => {
  if (ev.submitter && ev.submitter.value === "cancel") return;
  ev.preventDefault();
  try {
    await api(`/api/goals/${state.contribGoal}/contribute`, {
      method: "POST",
      body: { amount: formContrib.amount.value, note: formContrib.note.value },
    });
    dlgContrib.close();
    render();
  } catch (e) {
    $("#contrib-error").textContent = e.message;
  }
});

/* ---------- settle up ---------- */
const dlgSettle = $("#dlg-settle");

function openSettle() {
  const bal = window._dash.balance;
  if (bal.settled) return;
  $("#settle-summary").textContent =
    `${bal.owes.name} pays ${bal.owed.name} ${fmt(bal.amount)}.`;
  $("#settle-error").textContent = "";
  dlgSettle.showModal();
}

$("#form-settle").addEventListener("submit", async (ev) => {
  if (ev.submitter && ev.submitter.value === "cancel") return;
  ev.preventDefault();
  const bal = window._dash.balance;
  try {
    // Paid by the ower with payer share 0% — the full amount credits the other
    // person, which exactly offsets the outstanding balance.
    await api("/api/transactions", {
      method: "POST",
      body: {
        date: todayISO(),
        amount: bal.amount,
        description: `Settlement — ${bal.owes.name} → ${bal.owed.name}`,
        category: "Settlement",
        paid_by: bal.owes.id,
        is_shared: true,
        payer_share_pct: 0,
        source: "settlement",
      },
    });
    dlgSettle.close();
    render();
  } catch (e) {
    $("#settle-error").textContent = e.message;
  }
});

boot();
