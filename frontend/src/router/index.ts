import { createRouter, createWebHistory, type RouteRecordRaw } from "vue-router";
import { useAuthStore } from "@/stores/auth";
import AppShell from "@/layouts/AppShell.vue";
import JobsView from "@/views/JobsView.vue";
import SearchView from "@/views/SearchView.vue";
import AccountView from "@/views/AccountView.vue";
import LlmView from "@/views/LlmView.vue";
import TelegramView from "@/views/TelegramView.vue";
import ResumeView from "@/views/ResumeView.vue";
import InterestsView from "@/views/InterestsView.vue";
import CompaniesView from "@/views/CompaniesView.vue";
import ProfileView from "@/views/ProfileView.vue";
import PositionDetailView from "@/views/PositionDetailView.vue";
import CompanyDetailView from "@/views/CompanyDetailView.vue";
import AuthView from "@/views/auth/AuthView.vue";
import ForgotPasswordView from "@/views/auth/ForgotPasswordView.vue";
import SetNewPasswordView from "@/views/auth/SetNewPasswordView.vue";

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
      { path: "resume", component: ResumeView, meta: { title: "Resume", subtitle: "Current resume used for matching" } },
      { path: "profile", component: ProfileView, meta: { title: "Profile", subtitle: "Reusable application details" } },
      { path: "companies", component: CompaniesView, meta: { title: "Companies", subtitle: "Career sites included in each scan" } },
      { path: "interests", component: InterestsView, meta: { title: "Interests", subtitle: "Role preferences and scoring thresholds" } },
      { path: "llm", component: LlmView, meta: { title: "LLM provider", subtitle: "Model provider, API key, and models used for scoring" } },
      { path: "telegram", component: TelegramView, meta: { title: "Telegram", subtitle: "Daily report delivery" } },
      { path: "account", component: AccountView, meta: { title: "Account", subtitle: "Change your login password" } },
    ],
  },
  // Detail pages are standalone (their own topbar, no sidebar) to match the classic
  // pages and keep print-to-PDF isolation simple; auth-guarded like the rest of /app.
  { path: "/app/positions/:id", component: PositionDetailView, meta: { requiresAuth: true } },
  { path: "/app/companies/:id", component: CompanyDetailView, meta: { requiresAuth: true } },
  // Auth screens — standalone, no auth guard (set-new-password redirects on a 401 itself).
  { path: "/app/login", component: AuthView, meta: { mode: "login" } },
  { path: "/app/register", component: AuthView, meta: { mode: "register" } },
  { path: "/app/forgot-password", component: ForgotPasswordView },
  { path: "/app/set-new-password", component: SetNewPasswordView },
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
      window.location.assign(`/app/login?next=${encodeURIComponent(to.fullPath)}`);
      return false;
    }
  }
  return true;
});
