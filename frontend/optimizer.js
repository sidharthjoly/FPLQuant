import { api } from "./api.js";
import { clear, jerseyIcon } from "./components.js";
import { playerLink } from "./explorer.js";
import { kitFor } from "./kits.js";
import { updateHero } from "./main.js";

const POSITION_ORDER = [1, 2, 3, 4];
const PITCH_ROW_Y = { 1: 77, 2: 53, 3: 29, 4: 5 }; // % from top; attack faces up
const FDR_COLORS = { 1: "#5fc9a3", 2: "#7cc39a", 3: "#c2b487", 4: "#d69279", 5: "#e0736f" };

const form = document.getElementById("optimizer-form");
const riskToggle = document.getElementById("risk-toggle");
const riskSwitch = document.getElementById("risk-switch");
const riskCollapse = document.getElementById("risk-collapse");
const riskAversionInput = document.getElementById("risk-aversion");
const riskAversionThumb = document.getElementById("risk-aversion-thumb");
const riskAversionLabel = document.getElementById("risk-aversion-label");
const injuryWeightInput = document.getElementById("injury-weight");
const injuryWeightThumb = document.getElementById("injury-weight-thumb");
const injuryWeightLabel = document.getElementById("injury-weight-label");
const curvePath = document.getElementById("risk-curve-path");
const curveFill = document.getElementById("risk-curve-fill");
const curveDot = document.getElementById("risk-curve-dot");
const buildBtn = document.getElementById("build-btn");
const buildLabel = document.getElementById("build-label");
const statusEl = document.getElementById("optimizer-status");
const resultsEl = document.getElementById("optimizer-results");
const kpisEl = document.getElementById("optimizer-kpis");
const chipsEl = document.getElementById("pitch-chips");
const formationLabelEl = document.getElementById("pitch-formation-label");
const pitchBodyEl = document.getElementById("pitch-body");
const dugoutRowEl = document.getElementById("dugout-row");

let riskOn = false;

riskToggle.addEventListener("click", () => {
  riskOn = !riskOn;
  riskSwitch.classList.toggle("on", riskOn);
  riskCollapse.classList.toggle("open", riskOn);
});

function aversionLabelFor(v) {
  if (v < 1) return "Bold";
  if (v < 2.5) return "Balanced";
  if (v < 4) return "Careful";
  return "Safe";
}
function injuryLabelFor(v) {
  if (v < 0.5) return "Relaxed";
  if (v < 1.3) return "Sensible";
  return "Cautious";
}

function updateAversionVisuals() {
  const v = Number(riskAversionInput.value);
  riskAversionThumb.style.left = `${(v / 5) * 100}%`;
  riskAversionLabel.textContent = aversionLabelFor(v);
  updateRiskCurve(v);
}
function updateInjuryVisuals() {
  const v = Number(injuryWeightInput.value);
  injuryWeightThumb.style.left = `${(v / 2) * 100}%`;
  injuryWeightLabel.textContent = injuryLabelFor(v);
}

// A purely illustrative risk/reward shape — not a fit to real backtested
// data, just a visual cue for "more caution flattens the curve" as the
// slider moves, matching the sidebar's own framing ("risk vs reward").
function updateRiskCurve(a) {
  const pts = [];
  for (let i = 0; i <= 12; i++) {
    const x = i / 12;
    const y = Math.pow(x, 0.42 + a * 0.11) * (1 - x * 0.16 * (a / 5));
    pts.push([x * 260, 88 - y * 78]);
  }
  const path = pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(" ");
  curvePath.setAttribute("d", path);
  curveFill.setAttribute("d", `${path} L260 88 L0 88 Z`);
  const dotIndex = Math.min(12, Math.max(0, Math.round(((260 * (1 - a / 5) * 0.82 + 24) / 260) * 12)));
  curveDot.setAttribute("cx", pts[dotIndex][0]);
  curveDot.setAttribute("cy", pts[dotIndex][1]);
}

riskAversionInput.addEventListener("input", updateAversionVisuals);
injuryWeightInput.addEventListener("input", updateInjuryVisuals);
updateAversionVisuals();
updateInjuryVisuals();

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  statusEl.textContent = "Solving…";
  statusEl.classList.remove("error");
  buildBtn.classList.add("solving");
  buildLabel.textContent = "Solving…";
  resultsEl.hidden = true;

  const payload = {
    budget: Number(form.budget.value),
    max_per_club: Number(form.max_per_club.value),
    formation: form.formation.value === "auto" ? null : form.formation.value,
    risk_adjusted: riskOn,
    risk_aversion: Number(riskAversionInput.value),
    injury_weight: Number(injuryWeightInput.value),
  };

  try {
    const result = await api.optimize(payload);
    renderResult(result, riskOn);
    statusEl.textContent = "";
  } catch (err) {
    statusEl.textContent = `Could not build a squad: ${err.message}`;
    statusEl.classList.add("error");
  } finally {
    buildBtn.classList.remove("solving");
    buildLabel.textContent = "Build my squad";
  }
});

function renderResult(result, riskAdjusted) {
  clear(kpisEl);
  clear(pitchBodyEl);
  clear(dugoutRowEl);

  const xi = result.starting_xi;
  const bank = (Number(document.getElementById("budget").value) * 10 - result.total_cost) / 10;
  const chanceProduct = xi.starters.reduce((acc, p) => acc * p.chance_of_playing, 1);
  const squadRisk = 1 - chanceProduct;
  const riskColor = squadRisk < 0.15 ? "var(--fq-up)" : squadRisk < 0.35 ? "var(--fq-warn)" : "var(--fq-down)";

  kpisEl.appendChild(
    kpi("Spent", `£${(result.total_cost / 10).toFixed(1)}m`, `£${bank.toFixed(1)}m left in the bank`)
  );
  kpisEl.appendChild(
    kpi(
      "XI next match",
      xi.starting_predicted_points.toFixed(1),
      riskAdjusted ? "risk-adjusted, captain doubled" : "captain doubled",
      true
    )
  );
  kpisEl.appendChild(
    kpi("Shape", xi.formation, form.formation.value === "auto" ? "best of the eight" : "your pick")
  );
  kpisEl.appendChild(
    kpiColored("Squad risk", `${Math.round(squadRisk * 100)}%`, "chance someone misses out", riskColor)
  );

  formationLabelEl.textContent = `Starting XI · ${xi.formation}`;
  chipsEl.textContent = `Bench Boost +${xi.bench_boost_value.toFixed(1)} · Triple Captain +${xi.triple_captain_value.toFixed(1)}`;

  renderPitch(xi);
  renderDugout(xi.bench);

  updateHero({
    value: `£${(result.total_cost / 10).toFixed(1)}m`,
    points: xi.starting_predicted_points.toFixed(1),
    captain: xi.captain.web_name,
  });

  resultsEl.hidden = false;
}

function kpi(label, value, note, accent = false) {
  const el = document.createElement("div");
  el.className = "fq-kpi";
  el.innerHTML = "";
  const labelEl = document.createElement("div");
  labelEl.className = "fq-kpi__label";
  labelEl.textContent = label;
  const valueEl = document.createElement("div");
  valueEl.className = accent ? "fq-kpi__value fq-kpi__value--accent" : "fq-kpi__value";
  valueEl.textContent = value;
  const noteEl = document.createElement("div");
  noteEl.className = "fq-kpi__note";
  noteEl.textContent = note;
  el.appendChild(labelEl);
  el.appendChild(valueEl);
  el.appendChild(noteEl);
  return el;
}

function kpiColored(label, value, note, color) {
  const el = kpi(label, value, note);
  el.querySelector(".fq-kpi__value").style.color = color;
  return el;
}

function renderPitch(xi) {
  const byPosition = {};
  for (const player of xi.starters) (byPosition[player.element_type] ??= []).push(player);
  for (const players of Object.values(byPosition)) {
    players.sort((a, b) => b.predicted_points - a.predicted_points);
  }

  for (const position of POSITION_ORDER) {
    const players = byPosition[position];
    if (!players) continue;
    players.forEach((player, i) => {
      pitchBodyEl.appendChild(
        pitchPlayerEl(player, xi, ((i + 1) / (players.length + 1)) * 100, PITCH_ROW_Y[position])
      );
    });
  }
}

function pitchPlayerEl(player, xi, xPct, yPct) {
  const wrap = document.createElement("div");
  wrap.className = "fq-pitch-player";
  wrap.style.left = `${xPct}%`;
  wrap.style.top = `${yPct}%`;
  playerLink(wrap, player.player_id, player.web_name);

  const jerseyWrap = document.createElement("div");
  jerseyWrap.className = "fq-pitch-player__jersey-wrap";
  jerseyWrap.appendChild(jerseyIcon(kitFor(player.team_short_name), 46));

  const isCaptain = player.player_id === xi.captain.player_id;
  const isVice = player.player_id === xi.vice_captain.player_id;
  const badge = document.createElement("span");
  badge.className = "fq-pitch-player__badge";
  if (isCaptain || isVice) {
    badge.classList.add("show");
    badge.textContent = isCaptain ? "C" : "VC";
  }
  jerseyWrap.appendChild(badge);
  wrap.appendChild(jerseyWrap);

  const card = document.createElement("div");
  card.className = "fq-pitch-player__card";
  const info = document.createElement("div");
  info.className = "fq-pitch-player__info";
  const name = document.createElement("div");
  name.className = "fq-pitch-player__name";
  name.textContent = player.web_name;
  const line = document.createElement("div");
  line.className = "fq-pitch-player__line";
  line.textContent = `£${(player.now_cost / 10).toFixed(1)}m · ${player.predicted_points.toFixed(1)} pts`;
  info.appendChild(name);
  info.appendChild(line);
  card.appendChild(info);
  const fdrStrip = document.createElement("div");
  fdrStrip.className = "fq-pitch-player__fdr";
  fdrStrip.style.background = FDR_COLORS[player.fixture_difficulty] || "var(--fq-line)";
  card.appendChild(fdrStrip);
  wrap.appendChild(card);

  return wrap;
}

function renderDugout(bench) {
  for (const player of bench) {
    const row = document.createElement("div");
    row.className = "fq-bench-player";
    playerLink(row, player.player_id, player.web_name);

    const jerseyWrap = document.createElement("div");
    jerseyWrap.className = "fq-bench-player__jersey";
    jerseyWrap.appendChild(jerseyIcon(kitFor(player.team_short_name), 24));
    row.appendChild(jerseyWrap);

    const info = document.createElement("div");
    info.style.minWidth = "0";
    const name = document.createElement("div");
    name.className = "fq-bench-player__name";
    name.textContent = player.web_name;
    const line = document.createElement("div");
    line.className = "fq-bench-player__line";
    line.textContent = `£${(player.now_cost / 10).toFixed(1)}m`;
    info.appendChild(name);
    info.appendChild(line);
    row.appendChild(info);

    dugoutRowEl.appendChild(row);
  }
}
