import { createRouter, createWebHistory, type RouteRecordRaw } from "vue-router";
import { useAuthStore } from "@/stores/auth";
import AppShell from "@/layouts/AppShell.vue";
import JobsView from "@/views/JobsView.vue";
import SearchView from "@/views/SearchView.vue";
import PlaceholderView from "@/views/PlaceholderView.vue";

// Pass 1: the SPA owns only /app/*. The dashboard's old hash panels (#jobs, #resume, …)
// become child routes here; the other panels are placeholders until later passes.
// /, /login, /positions/:id, /companies/:id stay server-rendered (Jinja) for now.
const routes: RouteRecordRaw[] = [
  {
    path: "/app",
    component: AppShell,
    meta: { requiresAuth: true },
    children: [
      { path: "", redirect: "/app/jobs" },
      { path: "jobs", component: JobsView, meta: { title: "Job lists", subtitle: "Latest ranked positions from your scans" } },
      { path: "search", component: SearchView, meta: { title: "Search for Job", subtitle: "Look up a posting by its URL in your job list" } },
      { path: "resume", component: PlaceholderView, meta: { title: "Resume", subtitle: "Coming soon" } },
      { path: "profile", component: PlaceholderView, meta: { title: "Profile", subtitle: "Coming soon" } },
      { path: "companies", component: PlaceholderView, meta: { title: "Companies", subtitle: "Coming soon" } },
      { path: "interests", component: PlaceholderView, meta: { title: "Interests", subtitle: "Coming soon" } },
      { path: "llm", component: PlaceholderView, meta: { title: "LLM provider", subtitle: "Coming soon" } },
      { path: "telegram", component: PlaceholderView, meta: { title: "Telegram", subtitle: "Coming soon" } },
      { path: "account", component: PlaceholderView, meta: { title: "Account", subtitle: "Coming soon" } },
    ],
  },
  // Any other in-SPA path lands on the jobs view.
  { path: "/:pathMatch(.*)*", redirect: "/app/jobs" },
];

export const router = createRouter({
  history: createWebHistory(),
  routes,
});

// Initial/deep-link auth: confirm the session via GET /api/auth/me before entering
// /app/*; redirect to the server-rendered /login on 401. Mid-session 401s are handled
// in the API client.
router.beforeEach(async (to) => {
  if (to.matched.some((r) => r.meta.requiresAuth)) {
    const auth = useAuthStore();
    const ok = await auth.ensureAuth();
    if (!ok) {
      window.location.assign(`/login?next=${encodeURIComponent(to.fullPath)}`);
      return false;
    }
  }
  return true;
});
