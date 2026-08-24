import { api } from "./api.js";
import { clear, jerseyIcon, playerMetaLine } from "./components.js";
import { playerLink } from "./explorer.js";
import { kitFor } from "./kits.js";

const form = document.getElementById("transfers-form");
const riskToggle = document.getElementById("transfer-risk-toggle");
const riskSwitch = document.getElementById("transfer-risk-switch");
const riskCollapse = document.getElementById("transfer-risk-collapse");
const riskAversionInput = document.getElementById("transfer-risk-aversion");
const riskAversionThumb = document.getElementById("transfer-risk-aversion-thumb");
const riskAversionLabel = document.getElementById("transfer-risk-aversion-label");
const injuryWeightInput = document.getElementById("transfer-injury-weight");
const injuryWeightThumb = document.getElementById("transfer-injury-weight-thumb");
const injuryWeightLabel = document.getElementById("transfer-injury-weight-label");
const planBtn = document.getElementById("plan-btn");
const planLabel = document.getElementById("plan-label");
const statusEl = document.getElementById("transfers-status");
const resultsEl = document.getElementById("transfers-results");
const kpisEl = document.getElementById("transfers-kpis");
const verdictEl = document.getElementById("transfers-verdict");
const pairsEl = document.getElementById("transfers-pairs");
const currentSquadEl = document.getElementById("transfers-current-squad");

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

riskAversionInput.addEventListener("input", () => {
  const v = Number(riskAversionInput.value);
  riskAversionThumb.style.left = `${(v / 5) * 100}%`;
  riskAversionLabel.textContent = aversionLabelFor(v);
});
injuryWeightInput.addEventListener("input", () => {
  const v = Number(injuryWeightInput.value);
  injuryWeightThumb.style.left = `${(v / 2) * 100}%`;
  injuryWeightLabel.textContent = injuryLabelFor(v);
});
riskAversionInput.dispatchEvent(new Event("input"));
injuryWeightInput.dispatchEvent(new Event("input"));

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  statusEl.textContent = "Planning transfers…";
  statusEl.classList.remove("error");
  planBtn.classList.add("solving");
  planLabel.textContent = "Planning…";
  resultsEl.hidden = true;

  const payload = {
    fpl_team_id: Number(form.fpl_team_id.value),
    free_transfers: Number(form.free_transfers.value),
    chip: form.chip.value,
    max_per_club: Number(form.max_per_club.value),
    risk_adjusted: riskOn,
    risk_aversion: Number(riskAversionInput.value),
    injury_weight: Number(injuryWeightInput.value),
  };

  try {
    const result = await api.planTransfers(payload);
    renderResult(result);
    statusEl.textContent = "";
  } catch (err) {
    statusEl.textContent = `Could not plan transfers: ${err.message}`;
    statusEl.classList.add("error");
  } finally {
    planBtn.classList.remove("solving");
    planLabel.textContent = "Plan my transfers";
  }
});

function renderResult(result) {
  clear(kpisEl);
  clear(pairsEl);
  clear(currentSquadEl);

  kpisEl.appendChild(kpi("Team", result.team_name));
  kpisEl.appendChild(kpi("In the bank", `£${(result.bank / 10).toFixed(1)}m`));
  kpisEl.appendChild(
    kpi("Hit", result.hit_cost ? `−${result.hit_cost} pts` : "None", result.hit_cost ? "var(--fq-down)" : "var(--fq-dim)")
  );
  kpisEl.appendChild(kpi("Net gain", `+${result.points_gain_after_hit.toFixed(1)} pts`, "var(--fq-up)"));

  renderVerdict(result);
  renderPairs(result.transfers);
  renderCurrentSquad(result.current_squad);

  resultsEl.hidden = false;
}

function kpi(label, value, color) {
  const el = document.createElement("div");
  el.className = "fq-kpi";
  const labelEl = document.createElement("div");
  labelEl.className = "fq-kpi__label";
  labelEl.textContent = label;
  const valueEl = document.createElement("div");
  valueEl.className = "fq-kpi__value";
  valueEl.textContent = value;
  if (color) valueEl.style.color = color;
  el.appendChild(labelEl);
  el.appendChild(valueEl);
  return el;
}

function renderVerdict(result) {
  verdictEl.classList.remove("fq-verdict--positive", "fq-verdict--neutral");

  if (result.transfers_made === 0) {
    verdictEl.textContent = "No transfers recommended this week — your squad is already well positioned.";
    verdictEl.classList.add("fq-verdict--neutral");
    return;
  }

  const chipNote =
    result.chip === "none"
      ? result.hit_cost > 0
        ? `costing ${result.hit_cost} points beyond your free transfers`
        : "within your free transfers, no points cost"
      : `using your ${result.chip === "wildcard" ? "Wildcard" : "Free Hit"}, no points cost`;

  verdictEl.textContent =
    `Recommended: ${result.transfers_made} transfer${result.transfers_made === 1 ? "" : "s"} ` +
    `${chipNote} — a net gain of +${result.points_gain_after_hit.toFixed(1)} points for the next match.`;
  verdictEl.classList.add("fq-verdict--positive");
}

function renderPairs(transfers) {
  for (const pair of transfers) {
    const row = document.createElement("div");
    row.className = "fq-transfer-row";

    row.appendChild(transferSide("OUT", "fq-transfer-tag--out", pair.out));
    const arrow = document.createElement("div");
    arrow.className = "fq-transfer-arrow";
    arrow.appendChild(arrowIcon());
    row.appendChild(arrow);
    row.appendChild(transferSide("IN", "fq-transfer-tag--in", pair.player_in, true));

    pairsEl.appendChild(row);
  }
}

const SVG_NS = "http://www.w3.org/2000/svg";

function arrowIcon() {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("width", "20");
  svg.setAttribute("height", "20");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.6");
  const path = document.createElementNS(SVG_NS, "path");
  path.setAttribute("d", "M4 12h15M13 6l6 6-6 6");
  svg.appendChild(path);
  return svg;
}

function transferSide(labelText, tagClass, player, alignEnd = false) {
  const side = document.createElement("div");
  side.className = alignEnd ? "fq-transfer-side fq-transfer-side--in" : "fq-transfer-side";
  playerLink(side, player.player_id, player.web_name);

  const tag = document.createElement("span");
  tag.className = `fq-transfer-tag ${tagClass}`;
  tag.textContent = labelText;

  const info = document.createElement("div");
  const name = document.createElement("div");
  name.className = "fq-transfer-name";
  name.textContent = player.web_name;
  const meta = document.createElement("div");
  meta.className = "fq-transfer-meta";
  meta.textContent = playerMetaLine(player);
  info.appendChild(name);
  info.appendChild(meta);

  if (alignEnd) {
    side.appendChild(info);
    side.appendChild(tag);
  } else {
    side.appendChild(tag);
    side.appendChild(info);
  }
  return side;
}

function renderCurrentSquad(players) {
  for (const player of players) {
    const row = document.createElement("div");
    row.className = "fq-bench-player";
    row.style.flex = "0 1 220px";
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
    line.textContent = playerMetaLine(player);
    info.appendChild(name);
    info.appendChild(line);
    row.appendChild(info);

    currentSquadEl.appendChild(row);
  }
}
