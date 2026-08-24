import { API_BASE } from "./config.js";

async function request(path, options = {}) {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      detail = body.detail ?? detail;
    } catch {
      /* ignore parse failure, fall back to statusText */
    }
    throw new Error(detail);
  }
  return response.json();
}

export const api = {
  listPlayers: (params = {}) => {
    const qs = new URLSearchParams(params).toString();
    return request(`/players${qs ? `?${qs}` : ""}`);
  },
  getPlayer: (id) => request(`/players/${id}`),
  getPlayerHistory: (id) => request(`/players/${id}/history`),
  getSimilarPlayers: (id, params = {}) => {
    const qs = new URLSearchParams(params).toString();
    return request(`/players/${id}/similar${qs ? `?${qs}` : ""}`);
  },
  getForm: (params = {}) => {
    const qs = new URLSearchParams(params).toString();
    return request(`/form${qs ? `?${qs}` : ""}`);
  },
  getMarketMomentum: (params = {}) => {
    const qs = new URLSearchParams(params).toString();
    return request(`/market/momentum${qs ? `?${qs}` : ""}`);
  },
  optimize: (payload) =>
    request("/optimize", { method: "POST", body: JSON.stringify(payload) }),
  planTransfers: (payload) =>
    request("/transfers/plan", { method: "POST", body: JSON.stringify(payload) }),
  getProjections: (params = {}) => {
    const qs = new URLSearchParams(params).toString();
    return request(`/projections${qs ? `?${qs}` : ""}`);
  },
  planHorizon: (payload) =>
    request("/plan", { method: "POST", body: JSON.stringify(payload) }),
  getNextDeadline: () => request("/meta/next-deadline"),
};
