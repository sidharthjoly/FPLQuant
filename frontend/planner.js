// The multi-gameweek planner. Where the Optimizer answers "who should I own
// this weekend", this answers "what should I do between now and a month's
// time" — and the difference is the whole reason the view is a timeline rather
// than a pitch. A plan's interesting content is the *sequence*: the week it
// banks a transfer, the week it spends two, the week it decides your captain
// is worth tripling. A single-gameweek view cannot show any of that, because
// none of it exists inside one gameweek.
//
// Only the first gameweek is meant to be acted on. Everything after it is the
// justification for that first move, and gets re-solved next week with better
// information — so the timeline reads left to right with the first card
// highlighted, and the rest deliberately quieter.

import { api } from "./api.js";
import { clear, jerseyIcon, playerMetaLine } from "./components.js";
import { playerLink } from "./explorer.js";
import { kitFor } from "./kits.js";

const CHIP_LABELS = {
  wildcard: "Wildcard",
  bench_boost: "Bench Boost",
  triple_captain: "Triple Captain",
};

const form = document.getElementById("planner-form");
const horizonSeg = document.getElementById("horizon-seg");
const planBtn = document.getElementById("planner-btn");
const planLabel = document.getElementById("planner-label");
const statusEl = document.getElementById("planner-status");
const resultsEl = document.getElementById("planner-results");
const emptyEl = document.getElementById("planner-empty");
const kpisEl = document.getElementById("planner-kpis");
const verdictEl = document.getElementById("planner-verdict");
const timelineEl = document.getElementById("planner-timeline");
const squadKickerEl = document.getElementById("planner-squad-kicker");
const squadNoteEl = document.getElementById("planner-squad-note");
const xiEl = document.getElementById("planner-xi");
const benchEl = document.getElementById("planner-bench");

const chipInputs = [
  document.getElementById("chip-wildcard"),
  document.getElementById("chip-bench-boost"),
  document.getElementById("chip-triple-captain"),
];

let horizon = 5;

horizonSeg.addEventListener("click", (event) => {
  const btn = event.target.closest(".fq-seg__btn");
  if (!btn) return;
  horizon = Number(btn.dataset.horizon);
  for (const b of horizonSeg.querySelectorAll(".fq-seg__btn")) {
    b.classList.toggle("active", b === btn);
  }
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  statusEl.textContent = "Solving every gameweek together — this one takes a moment…";
  statusEl.classList.remove("error");
  planBtn.classList.add("solving");
  planLabel.textContent = "Planning…";
  resultsEl.hidden = true;
  emptyEl.hidden = true;

  const teamId = form.fpl_team_id.value.trim();
  const payload = {
    horizon,
    budget: Number(form.budget.value),
    max_per_club: Number(form.max_per_club.value),
    free_transfers: Number(form.free_transfers.value),
    chips: chipInputs.filter((input) => input.checked).map((input) => input.value),
  };
  if (teamId) payload.fpl_team_id = Number(teamId);

  try {
    const plan = await api.planHorizon(payload);
    renderPlan(plan);
    statusEl.textContent = "";
  } catch (err) {
    statusEl.textContent = `Could not build a plan: ${err.message}`;
    statusEl.classList.add("error");
    emptyEl.hidden = false;
  } finally {
    planBtn.classList.remove("solving");
    planLabel.textContent = "Plan my season";
  }
});

function renderPlan(plan) {
  clear(kpisEl);
  clear(timelineEl);

  const chipsPlayed = plan.gameweeks.filter((gw) => gw.chip);
  const events = plan.events;

  kpisEl.appendChild(
    kpi("Horizon", `GW${events[0]}–${events[events.length - 1]}`, `${events.length} gameweeks`)
  );
  kpisEl.appendChild(
    kpi("Expected points", plan.total_expected_points.toFixed(1), "captaincy and chips included", true)
  );
  kpisEl.appendChild(
    kpi(
      "Point hits",
      plan.total_hit_cost ? `−${plan.total_hit_cost}` : "None",
      plan.total_hit_cost ? "worth paying, per the solver" : "no transfer costs more than it earns"
    )
  );
  kpisEl.appendChild(
    kpi(
      "Chips",
      chipsPlayed.length ? String(chipsPlayed.length) : "—",
      chipsPlayed.length
        ? chipsPlayed.map((gw) => `${CHIP_LABELS[gw.chip]} GW${gw.event}`).join(", ")
        : "none scheduled"
    )
  );

  renderVerdict(plan);

  const best = Math.max(...plan.gameweeks.map((gw) => gw.expected_points), 1);
  plan.gameweeks.forEach((gameweek, index) => {
    timelineEl.appendChild(timelineCard(gameweek, index === 0, best, plan));
  });

  selectGameweek(plan, plan.gameweeks[0]);
  resultsEl.hidden = false;
  emptyEl.hidden = true;
}

function kpi(label, value, note, accent = false) {
  const el = document.createElement("div");
  el.className = "fq-kpi";
  const labelEl = document.createElement("div");
  labelEl.className = "fq-kpi__label";
  labelEl.textContent = label;
  const valueEl = document.createElement("div");
  valueEl.className = accent ? "fq-kpi__value fq-kpi__value--accent" : "fq-kpi__value";
  valueEl.textContent = value;
  el.appendChild(labelEl);
  el.appendChild(valueEl);
  if (note) {
    const noteEl = document.createElement("div");
    noteEl.className = "fq-kpi__note";
    noteEl.textContent = note;
    el.appendChild(noteEl);
  }
  return el;
}

/** The one line worth reading: what to do *this* week, and why the rest of the
 * plan is only there to justify it. */
function renderVerdict(plan) {
  const first = plan.gameweeks[0];
  verdictEl.classList.remove("fq-verdict--positive", "fq-verdict--neutral");

  const moves = first.transfers.length;
  const next = plan.gameweeks[1];
  let text;
  if (moves === 0) {
    // What you carry into next week is read off the plan rather than derived
    // here. The cap and the carry-over rule live in the solver, and a second
    // copy of them in the UI is a copy that will eventually disagree.
    text = `Do nothing in GW${first.event}. Banking your transfer is worth more than any move available this week`;
    text += next
      ? ` — you go into GW${next.event} with ${next.free_transfers_available} in hand.`
      : ".";
    verdictEl.classList.add("fq-verdict--neutral");
  } else {
    const named = first.transfers
      .map((t) => `${t.out.web_name} → ${t.in.web_name}`)
      .join(", ");
    const cost = first.hit_cost ? ` for a −${first.hit_cost} hit` : " within your free transfers";
    text = `In GW${first.event}: ${named}${cost}. Captain ${first.starting_xi.captain.web_name}.`;
    verdictEl.classList.add("fq-verdict--positive");
  }
  if (first.chip) text += ` Play your ${CHIP_LABELS[first.chip]}.`;
  text += " Only this week is meant to be acted on — re-run once the next round's news lands.";
  verdictEl.textContent = text;
}

function timelineCard(gameweek, isFirst, best, plan) {
  const card = document.createElement("button");
  card.type = "button";
  card.className = isFirst ? "fq-tl-card fq-tl-card--now" : "fq-tl-card";
  card.dataset.event = String(gameweek.event);

  const head = document.createElement("div");
  head.className = "fq-tl-card__head";
  const gw = document.createElement("span");
  gw.className = "fq-tl-card__gw";
  gw.textContent = `GW${gameweek.event}`;
  head.appendChild(gw);
  if (isFirst) {
    const now = document.createElement("span");
    now.className = "fq-tl-card__now";
    now.textContent = "now";
    head.appendChild(now);
  }
  card.appendChild(head);

  const points = document.createElement("div");
  points.className = "fq-tl-card__points";
  points.textContent = gameweek.expected_points.toFixed(1);
  card.appendChild(points);

  // A bar per gameweek, scaled against the best week in the plan. Turns the
  // shape of the plan — the double-gameweek spike, the blank trough, the week
  // a chip fires — into something readable at a glance.
  const track = document.createElement("div");
  track.className = "fq-tl-card__track";
  const fill = document.createElement("div");
  fill.className = "fq-tl-card__fill";
  fill.style.width = `${Math.max(4, (gameweek.expected_points / best) * 100)}%`;
  track.appendChild(fill);
  card.appendChild(track);

  if (gameweek.chip) {
    const chip = document.createElement("div");
    chip.className = "fq-tl-chip";
    chip.textContent = CHIP_LABELS[gameweek.chip];
    card.appendChild(chip);
  }

  const moves = document.createElement("div");
  moves.className = "fq-tl-card__moves";
  if (gameweek.transfers.length === 0) {
    moves.classList.add("fq-tl-card__moves--none");
    moves.textContent = "No transfers";
  } else {
    for (const move of gameweek.transfers) {
      const row = document.createElement("div");
      row.className = "fq-tl-move";
      const out = document.createElement("span");
      out.className = "fq-tl-move__out";
      out.textContent = move.out.web_name;
      const arrow = document.createElement("span");
      arrow.className = "fq-tl-move__arrow";
      arrow.textContent = "→";
      const inEl = document.createElement("span");
      inEl.className = "fq-tl-move__in";
      inEl.textContent = move.in.web_name;
      row.append(out, arrow, inEl);
      moves.appendChild(row);
    }
  }
  card.appendChild(moves);

  const foot = document.createElement("div");
  foot.className = "fq-tl-card__foot";
  const captain = document.createElement("span");
  captain.textContent = `C ${gameweek.starting_xi.captain.web_name}`;
  foot.appendChild(captain);
  const cost = document.createElement("span");
  if (gameweek.hit_cost) {
    cost.textContent = `−${gameweek.hit_cost}`;
    cost.style.color = "var(--fq-down)";
  } else {
    cost.textContent = `${gameweek.free_transfers_available} FT`;
  }
  foot.appendChild(cost);
  card.appendChild(foot);

  const blanking = gameweek.starting_xi.starters.filter((p) => p.predicted_points === 0).length;
  if (blanking > 5) {
    // A part-played or heavily blanked round projects most of the XI to zero.
    // That is correct and reads exactly like a broken model if nobody says so.
    const note = document.createElement("div");
    note.className = "fq-tl-card__blank";
    note.textContent = `${blanking} have no fixture`;
    card.appendChild(note);
  }

  card.addEventListener("click", () => selectGameweek(plan, gameweek));
  return card;
}

/** Show one gameweek's fifteen underneath the timeline. The squad changes week
 * to week, so the timeline alone can't tell you what you'd actually own. */
function selectGameweek(plan, gameweek) {
  for (const card of timelineEl.querySelectorAll(".fq-tl-card")) {
    card.classList.toggle("is-selected", Number(card.dataset.event) === gameweek.event);
  }

  clear(xiEl);
  clear(benchEl);

  squadKickerEl.textContent = `GW${gameweek.event} squad`;
  const chipNote = gameweek.chip ? ` · ${CHIP_LABELS[gameweek.chip]}` : "";
  squadNoteEl.textContent =
    `${gameweek.starting_xi.formation} · ` +
    `£${(gameweek.squad.reduce((sum, p) => sum + p.now_cost, 0) / 10).toFixed(1)}m · ` +
    `C ${gameweek.starting_xi.captain.web_name}, VC ${gameweek.starting_xi.vice_captain.web_name}` +
    chipNote;

  const captainId = gameweek.starting_xi.captain.player_id;
  const viceId = gameweek.starting_xi.vice_captain.player_id;
  for (const player of gameweek.starting_xi.starters) {
    let badge = null;
    if (player.player_id === captainId) badge = "C";
    else if (player.player_id === viceId) badge = "VC";
    xiEl.appendChild(squadRow(player, badge));
  }
  for (const player of gameweek.starting_xi.bench) {
    benchEl.appendChild(squadRow(player, null));
  }
}

function squadRow(player, badge) {
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
  if (badge) {
    const badgeEl = document.createElement("span");
    badgeEl.className = "fq-tl-badge";
    badgeEl.textContent = badge;
    name.appendChild(badgeEl);
  }
  const line = document.createElement("div");
  line.className = "fq-bench-player__line";
  line.textContent = playerMetaLine(player);
  info.appendChild(name);
  info.appendChild(line);
  row.appendChild(info);

  return row;
}
