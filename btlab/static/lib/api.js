// Every request of the UI goes through here: JSON bodies, the error format of §19, and 401 back to the login.

export class ApiError extends Error {
  constructor(status, body) {
    super(body && body.message ? body.message : `HTTP ${status}`);
    this.status = status;
    this.code = body && body.error ? body.error : `HTTP_${status}`;
    this.path = body ? body.path : undefined;
    this.body = body || {};
  }
}

export const UNAUTHORIZED = "btlab:unauthorized";

let offline = false;

export function setOffline(value) {
  // Offline, the app reads stored results only and writes nothing (§20.5).
  offline = value;
}

export async function api(method, path, body) {
  if (offline && method !== "GET") throw new ApiError(0, {error: "OFFLINE", message: ""});
  const options = {method, credentials: "same-origin", headers: {}};
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, options);
  } catch {
    // Neither btlab nor the saved results answered.
    throw new ApiError(0, {error: "OFFLINE", message: ""});
  }
  if (response.status === 401 && path !== "/api/login") window.dispatchEvent(new Event(UNAUTHORIZED));
  const json = (response.headers.get("Content-Type") || "").startsWith("application/json");
  const content = json ? await response.json() : null;
  if (!response.ok) throw new ApiError(response.status, content);
  return content;
}

export const get = (path) => api("GET", path);
export const post = (path, body = {}) => api("POST", path, body);
export const put = (path, body) => api("PUT", path, body);
export const patch = (path, body) => api("PATCH", path, body);
export const remove = (path) => api("DELETE", path);
