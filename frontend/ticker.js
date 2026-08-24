import { api } from "./api.js";
import { clear } from "./components.js";
import { playerLink } from "./explorer.js";

const TOP_N = 15;
const SVG_NS = "http://www.w3.org/2000/svg";

const segEl = document.getElementById("lookback-seg");
const lookbackLabelEl = document.getElementById("lookback-label");
const stripEl = document.getElementById("ticker-strip");
const emptyEl = document.getElementById("ticker-empty");

let lookback = 5;

segEl.addEventListener("click", (event) => {
  const btn = event.target.closest(".fq-seg__btn");
  if (!btn) return;
  lookback = Number(btn.dataset.lookback);
  lookbackLabelEl.textContent = String(lookback);
  for (const b of segEl.querySelectorAll(".fq-seg__btn")) b.classList.toggle("active", b === btn);
  loadTicker();
});

export async function loadTicker() {
  clear(stripEl);
  emptyEl.hidden = true;

  const momentum = await api.getMarketMomentum({ top: TOP_N, lookback });

  if (momentum.length === 0) {
    emptyEl.hidden = false;
    return;
  }

  const histories = await Promise.all(
    momentum.map((m) => api.getPlayerHistory(m.player_id).catch(() => []))
  );

  momentum.forEach((m, i) => {
    stripEl.appendChild(buildCard(m, histories[i]));
  });
}

function buildCard(momentum, history) {
  const priceUp = momentum.price_change >= 0;
  const color = priceUp ? "var(--fq-up)" : "var(--fq-down)";

  const card = document.createElement("div");
  card.className = "fq-market-card";
  playerLink(card, momentum.player_id, momentum.web_name);

  const head = document.createElement("div");
  head.className = "fq-market-card__head";
  const name = document.createElement("div");
  name.className = "fq-market-card__name";
  name.textContent = momentum.web_name;
  const team = document.createElement("div");
  team.className = "fq-market-card__team";
  team.textContent = momentum.team_short_name || "";
  head.appendChild(name);
  head.appendChild(team);
  card.appendChild(head);

  const chartWrap = document.createElement("div");
  chartWrap.className = "fq-market-card__chart";
  chartWrap.appendChild(buildMarketChart(history.map((h) => h.total_points), color));
  card.appendChild(chartWrap);

  card.appendChild(
    row(
      "Price",
      `${priceUp ? "▲" : "▼"} £${Math.abs(momentum.price_change / 10).toFixed(1)}m`,
      priceUp ? "good" : "bad"
    )
  );
  const ownUp = momentum.ownership_change >= 0;
  card.appendChild(
    row(
      "Ownership",
      `${ownUp ? "▲" : "▼"} ${Math.abs(momentum.ownership_change_pct * 100).toFixed(1)}%`,
      ownUp ? "good" : "bad"
    )
  );

  return card;
}

function row(label, value, cls) {
  const el = document.createElement("div");
  el.className = "fq-market-card__row";
  const labelEl = document.createElement("span");
  labelEl.textContent = label;
  const valueEl = document.createElement("span");
  valueEl.className = cls;
  valueEl.textContent = value;
  el.appendChild(labelEl);
  el.appendChild(valueEl);
  return el;
}

function buildMarketChart(values, color) {
  const width = 200;
  const height = 44;
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("width", "100%");
  svg.setAttribute("height", height);
  svg.style.display = "block";
  svg.style.overflow = "visible";

  if (values.length === 0) return svg;
  const plotValues = values.length === 1 ? [values[0], values[0]] : values;
  const min = Math.min(...plotValues);
  const max = Math.max(...plotValues);
  const range = max - min || 1;
  const step = (width - 8) / (plotValues.length - 1);
  const coords = plotValues.map((v, i) => [4 + i * step, 40 - ((v - min) / range) * 34]);

  const linePath = coords.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(" ");
  const fillPath = `${linePath} L${coords[coords.length - 1][0]} 44 L${coords[0][0]} 44 Z`;

  const fill = document.createElementNS(SVG_NS, "path");
  fill.setAttribute("d", fillPath);
  fill.setAttribute("fill", color);
  fill.setAttribute("opacity", "0.12");
  svg.appendChild(fill);

  const line = document.createElementNS(SVG_NS, "path");
  line.setAttribute("d", linePath);
  line.setAttribute("fill", "none");
  line.setAttribute("stroke", color);
  line.setAttribute("stroke-width", "2");
  line.setAttribute("stroke-linecap", "round");
  line.setAttribute("stroke-linejoin", "round");
  svg.appendChild(line);

  const [lastX, lastY] = coords[coords.length - 1];
  const dot = document.createElementNS(SVG_NS, "circle");
  dot.setAttribute("cx", lastX);
  dot.setAttribute("cy", lastY);
  dot.setAttribute("r", "3.2");
  dot.setAttribute("fill", color);
  dot.setAttribute("stroke", "var(--fq-surf)");
  dot.setAttribute("stroke-width", "2");
  svg.appendChild(dot);

  return svg;
}
