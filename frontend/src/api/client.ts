// Central fetch wrapper, replacing the inline `api()` helper from the Jinja templates.
// - Always sends the auth cookie (credentials: "include").
// - Sets JSON Content-Type, except for FormData (e.g. resume upload).
// - Normalizes FastAPI's `detail` (string OR 422 validation array) into one message.
// - Routes any 401 to the (still server-rendered) /login page, once.

export class ApiError extends Error {
  status: number;
  retryAfter: number | null;
  constructor(status: number, message: string, retryAfter: number | null = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.retryAfter = retryAfter;
  }
}

/** Mirror of the login.html `errorText` helper: FastAPI `detail` is a string for
 *  HTTPException but a list of {msg,...} for 422 validation errors. */
function errorText(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail.map((e) => (e && typeof e === "object" && "msg" in e ? (e as any).msg : String(e))).join("; ");
  }
  return "Request failed";
}

let redirectingToLogin = false;
function goToLogin(): void {
  if (redirectingToLogin) return;
  redirectingToLogin = true;
  const next = encodeURIComponent(window.location.pathname + window.location.search);
  window.location.assign(`/login?next=${next}`);
}

let redirectingToReset = false;
function goToSetNewPassword(): void {
  if (redirectingToReset) return;
  redirectingToReset = true;
  window.location.assign("/set-new-password");
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const opts: RequestInit = { method, credentials: "include", headers: {} };
  if (body instanceof FormData) {
    opts.body = body; // let the browser set the multipart boundary
  } else if (body !== undefined) {
    (opts.headers as Record<string, string>)["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }

  const res = await fetch(path, opts);

  if (res.status === 401) {
    goToLogin();
    throw new ApiError(401, "Not authenticated");
  }
  if (res.status === 204) {
    return undefined as T;
  }

  let payload: any = null;
  try {
    payload = await res.json();
  } catch {
    /* empty/non-JSON body */
  }

  // A user who logged in with a temporary password is gated until they pick a real one:
  // the backend 403s every protected route with this code. Funnel them to the
  // (server-rendered) set-new-password screen, the same way a 401 funnels to /login.
  if (res.status === 403 && payload?.detail === "password_change_required") {
    goToSetNewPassword();
    throw new ApiError(403, "password_change_required");
  }

  if (!res.ok) {
    const retry = res.headers.get("Retry-After");
    throw new ApiError(res.status, errorText(payload?.detail) || `Request failed (${res.status})`, retry ? Number(retry) : null);
  }
  return payload as T;
}

export const api = {
  get: <T>(path: string) => request<T>("GET", path),
  post: <T>(path: string, body?: unknown) => request<T>("POST", path, body),
  put: <T>(path: string, body?: unknown) => request<T>("PUT", path, body),
  patch: <T>(path: string, body?: unknown) => request<T>("PATCH", path, body),
  del: <T>(path: string) => request<T>("DELETE", path),
};
