import { defineStore } from "pinia";
import { ref } from "vue";
import { api, ApiError } from "@/api/client";
import type { UserOut } from "@/api/types";

// Auth state for the SPA. The httpOnly cookie isn't readable from JS, so we learn
// whether the session is valid by calling GET /api/auth/me. The router guard uses
// ensureAuth() on entry to /app/*; a mid-session 401 is handled in the API client
// (redirect to the still-server-rendered /login).
export const useAuthStore = defineStore("auth", () => {
  const user = ref<UserOut | null>(null);
  const status = ref<"unknown" | "authed" | "anon">("unknown");

  async function ensureAuth(): Promise<boolean> {
    if (status.value !== "unknown") return status.value === "authed";
    try {
      user.value = await api.get<UserOut>("/api/auth/me");
      // Logged in with a temporary password but haven't set a real one yet: the rest of
      // the app's API calls would 403, so funnel straight to the reset screen. (/me is
      // the one authed route that isn't gated, so we catch it here rather than via the
      // client's 403 handler.)
      if (user.value?.must_change_password) {
        window.location.assign("/set-new-password");
        return false;
      }
      status.value = "authed";
      return true;
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        status.value = "anon";
        return false;
      }
      throw e;
    }
  }

  return { user, status, ensureAuth };
});
