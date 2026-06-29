// Raw fetch for the auth screens. Deliberately NOT the shared api client: a 401 on a
// bad login must surface as an error message, not trigger the client's redirect-to-login.

export function errorText(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((e) => (e && typeof e === "object" && "msg" in e ? (e as { msg: string }).msg : String(e)))
      .filter(Boolean)
      .join("; ");
  }
  return "";
}

export interface AuthResponse {
  ok: boolean;
  status: number;
  data: { detail?: unknown; must_change_password?: boolean } | null;
}

export async function postJson(path: string, body: unknown): Promise<AuthResponse> {
  const res = await fetch(path, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  let data: AuthResponse["data"] = null;
  try {
    data = await res.json();
  } catch {
    /* empty / non-JSON body */
  }
  return { ok: res.ok, status: res.status, data };
}

export async function getMe(): Promise<{ must_change_password?: boolean } | null> {
  const res = await fetch("/api/auth/me", { credentials: "include" });
  return res.ok ? res.json() : null;
}

/** A safe post-login destination: only same-app /app paths, else /app. */
export function safeNext(next: unknown): string {
  return typeof next === "string" && next.startsWith("/app") ? next : "/app";
}
