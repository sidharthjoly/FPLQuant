// Which top-level panel is showing. Kept in its own module, importing nothing,
// so any panel can send the user to another one — the explorer opening a
// player's profile from a click on the pitch, say — without the panels having
// to import each other in a cycle. Anything that needs to *react* to a tab
// change (lazy-loading a panel's data, for instance) registers via
// onTabChange rather than being wired in here.

const tabs = document.querySelectorAll(".fq-nav__btn");
const panels = document.querySelectorAll(".panel");
const navSlider = document.getElementById("nav-slider");

const listeners = [];

export function onTabChange(listener) {
  listeners.push(listener);
}

export function setActiveTab(target) {
  const index = Array.from(tabs).findIndex((t) => t.dataset.tab === target);
  const tabWidth = tabs[0].getBoundingClientRect().width;
  navSlider.style.transform = `translateX(${index * tabWidth}px)`;

  for (const t of tabs) {
    const active = t.dataset.tab === target;
    t.classList.toggle("active", active);
    t.setAttribute("aria-selected", active ? "true" : "false");
  }
  for (const panel of panels) {
    panel.classList.toggle("active", panel.id === `panel-${target}`);
  }

  for (const listener of listeners) listener(target);
}

for (const tab of tabs) {
  tab.addEventListener("click", () => setActiveTab(tab.dataset.tab));
}

window.addEventListener("resize", () => {
  const active = document.querySelector(".fq-nav__btn.active");
  if (active) setActiveTab(active.dataset.tab);
});

setActiveTab("optimizer");
