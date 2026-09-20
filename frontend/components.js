const SVG_NS = "http://www.w3.org/2000/svg";

export function clear(el) {
  el.replaceChildren();
}

const JERSEY_PATH =
  "M13 7 L18 4 Q22 8 26 4 L31 7 L38 12 L34 18 L31 16 V38 Q22 40 13 38 V16 L10 18 L6 12 Z";
const JERSEY_COLLAR = "M18 4 Q22 8 26 4";

/** A simple jersey glyph colored in a club's home kit, used on the pitch
 * view and anywhere a player is represented without a photo. */
export function jerseyIcon([primary, secondary], size = 24) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 44 44");
  svg.setAttribute("width", size);
  svg.setAttribute("height", size);
  svg.style.display = "block";

  const body = document.createElementNS(SVG_NS, "path");
  body.setAttribute("d", JERSEY_PATH);
  body.setAttribute("fill", primary);
  body.setAttribute("stroke", secondary);
  body.setAttribute("stroke-width", "1.6");
  body.setAttribute("stroke-linejoin", "round");
  svg.appendChild(body);

  const collar = document.createElementNS(SVG_NS, "path");
  collar.setAttribute("d", JERSEY_COLLAR);
  collar.setAttribute("fill", "none");
  collar.setAttribute("stroke", secondary);
  collar.setAttribute("stroke-width", "1.6");
  svg.appendChild(collar);

  return svg;
}

/** A semicircular gauge, 0-100, for risk-style percentages. */
export function donutGauge(pct, color) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 140 92");
  svg.setAttribute("width", "100%");
  svg.setAttribute("height", "120");
  svg.style.display = "block";

  const track = document.createElementNS(SVG_NS, "path");
  track.setAttribute("d", "M16 82 A54 54 0 0 1 124 82");
  track.setAttribute("fill", "none");
  track.setAttribute("stroke", "var(--fq-line)");
  track.setAttribute("stroke-width", "9");
  track.setAttribute("stroke-linecap", "round");
  svg.appendChild(track);

  const arcLength = 170;
  const fill = document.createElementNS(SVG_NS, "path");
  fill.setAttribute("d", "M16 82 A54 54 0 0 1 124 82");
  fill.setAttribute("fill", "none");
  fill.setAttribute("stroke", color);
  fill.setAttribute("stroke-width", "9");
  fill.setAttribute("stroke-linecap", "round");
  fill.setAttribute("stroke-dasharray", String(arcLength));
  fill.setAttribute("stroke-dashoffset", String(arcLength - (pct / 100) * arcLength));
  svg.appendChild(fill);

  const text = document.createElementNS(SVG_NS, "text");
  text.setAttribute("x", "70");
  text.setAttribute("y", "76");
  text.setAttribute("text-anchor", "middle");
  text.setAttribute("font-size", "26");
  text.setAttribute("font-weight", "500");
  text.setAttribute("fill", "var(--fq-text)");
  text.textContent = `${Math.round(pct)}%`;
  svg.appendChild(text);

  return svg;
}

/** Cost/points plus, when available, next opponent/venue/difficulty and
 * playing-chance — the "will they have a good game against this opponent at
 * this venue" context behind why a player was picked, benched, or offered
 * as a transfer target.
 *
 * "to play" and "to start" are deliberately different labels for different
 * numbers. `chance_of_playing` is fitness, so it reads "to play" and is only
 * worth showing when it is short of certain. `start_probability` is selection
 * odds, sent by every surface that asks the engine for its projection — the
 * planner, and as of 2026-09-20 the optimizer and transfer planner too. A
 * fully fit rotation risk is 100% to play and may still be 46% to start, so
 * labelling that "to play" told the user their striker was injured when he
 * was not. A null stays unrendered rather than reading as zero: it means the
 * projection behind it does not model selection. */
export function playerMetaLine(player) {
  let text = `£${(player.now_cost / 10).toFixed(1)}m · ${player.predicted_points.toFixed(2)} pts`;
  if (player.next_opponent) {
    const venue = player.next_opponent_is_home ? "H" : "A";
    text += ` · vs ${player.next_opponent} (${venue})`;
    if (player.fixture_difficulty) {
      text += ` · FDR ${player.fixture_difficulty}`;
    }
  }
  if (player.chance_of_playing < 1) {
    text += ` · ${Math.round(player.chance_of_playing * 100)}% to play`;
  }
  if (player.start_probability != null) {
    text += ` · ${Math.round(player.start_probability * 100)}% to start`;
  }
  return text;
}
