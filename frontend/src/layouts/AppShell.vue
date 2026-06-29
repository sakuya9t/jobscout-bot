<template>
  <main class="dashboard" :class="{ 'nav-open': mobileNavOpen }">
    <header class="mobile-bar">
      <button
        class="menu-toggle"
        type="button"
        :aria-expanded="mobileNavOpen"
        aria-label="Toggle navigation menu"
        @click="mobileNavOpen = !mobileNavOpen"
      >
        <svg v-if="!mobileNavOpen" width="22" height="22" viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path d="M3 6h18M3 12h18M3 18h18" stroke="currentColor" stroke-width="2" stroke-linecap="round" />
        </svg>
        <svg v-else width="22" height="22" viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path d="M6 6l12 12M18 6L6 18" stroke="currentColor" stroke-width="2" stroke-linecap="round" />
        </svg>
      </button>
      <RouterLink class="brand-mark" to="/app/jobs">JobScout</RouterLink>
    </header>

    <aside class="sidebar">
      <div class="sidebar-top">
        <div>
          <RouterLink class="brand-mark" to="/app/jobs">JobScout</RouterLink>
          <div class="account">{{ auth.user?.email }}</div>
        </div>
        <ThemeToggle />
      </div>

      <div class="nav-stack">
        <div class="nav-section">
          <div class="nav-section-title">Workspace</div>
          <RouterLink to="/app/jobs" class="nav-button" active-class="active">Job lists</RouterLink>
          <RouterLink to="/app/search" class="nav-button" active-class="active">Search for Job</RouterLink>
        </div>

        <div class="nav-section" :class="{ collapsed: !applicationOpen }">
          <button class="nav-section-toggle" type="button" :aria-expanded="applicationOpen" @click="applicationOpen = !applicationOpen">
            Application settings
          </button>
          <div class="nav-section-body">
            <RouterLink to="/app/resume" class="nav-button" active-class="active">Resume</RouterLink>
            <RouterLink to="/app/profile" class="nav-button" active-class="active">Profile</RouterLink>
            <RouterLink to="/app/companies" class="nav-button" active-class="active">Companies</RouterLink>
            <RouterLink to="/app/interests" class="nav-button" active-class="active">Interests</RouterLink>
          </div>
        </div>

        <div class="nav-section" :class="{ collapsed: !systemOpen }">
          <button class="nav-section-toggle" type="button" :aria-expanded="systemOpen" @click="systemOpen = !systemOpen">
            System settings
          </button>
          <div class="nav-section-body">
            <RouterLink to="/app/llm" class="nav-button" active-class="active">LLM provider</RouterLink>
            <RouterLink to="/app/telegram" class="nav-button" active-class="active">Telegram</RouterLink>
            <RouterLink to="/app/account" class="nav-button" active-class="active">Account</RouterLink>
          </div>
        </div>
      </div>

      <button class="ghost logout" type="button" @click="logout">Log out</button>
    </aside>

    <section class="workspace">
      <div class="view-header">
        <div>
          <h1>{{ title }}</h1>
          <div class="muted">{{ subtitle }}</div>
        </div>
      </div>
      <RouterView />
    </section>
  </main>
</template>

<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { useRoute } from "vue-router";
import { api } from "@/api/client";
import { useAuthStore } from "@/stores/auth";
import ThemeToggle from "@/components/ThemeToggle.vue";

const route = useRoute();
const auth = useAuthStore();

const title = computed(() => (route.meta.title as string) ?? "JobScout");
const subtitle = computed(() => (route.meta.subtitle as string) ?? "");

// Auto-open the section containing the active panel (collapsed otherwise, matching the
// classic dashboard's nav behavior).
const applicationOpen = ref(false);
const systemOpen = ref(false);
// On narrow screens the sidebar is hidden behind a menu toggle; opening it hides the
// content, so navigating to a panel must close it again to reveal the content.
const mobileNavOpen = ref(false);
watch(
  () => route.path,
  (p) => {
    if (["/app/resume", "/app/profile", "/app/companies", "/app/interests"].includes(p)) applicationOpen.value = true;
    if (["/app/llm", "/app/telegram", "/app/account"].includes(p)) systemOpen.value = true;
    mobileNavOpen.value = false;
  },
  { immediate: true },
);

async function logout(): Promise<void> {
  try {
    await api.post("/api/auth/logout");
  } finally {
    window.location.assign("/app/login");
  }
}
</script>

<style scoped>
.dashboard {
  max-width: none;
  min-height: 100vh;
  margin: 0;
  padding: 0;
  display: grid;
  grid-template-columns: 240px minmax(0, 1fr);
}
.sidebar {
  background: var(--sidebar);
  color: var(--sidebar-ink);
  border-right: 1px solid var(--line);
  height: 100vh;
  position: sticky;
  top: 0;
  align-self: start;
  overflow-y: auto;
  padding: 16px 12px;
  display: flex;
  flex-direction: column;
  gap: 22px;
}
.sidebar-top {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 14px;
  padding: 0 4px 18px;
  border-bottom: 1px solid var(--line);
}
.brand-mark { display: inline-block; color: var(--brand-primary); font-size: 18px; line-height: 28px; font-weight: 700; text-decoration: none; }
.account { color: var(--muted); font-size: 11px; line-height: 16px; word-break: break-word; }
.nav-stack { display: grid; gap: 18px; }
.nav-section { display: grid; gap: 6px; }
.nav-section-title {
  color: var(--text-sidebar-section); font-size: 11px; line-height: 16px; font-weight: 600;
  text-transform: uppercase; letter-spacing: 0.08em; padding: 12px 16px 4px;
}
.nav-section-toggle {
  width: 100%; min-height: 32px; padding: 8px 16px 4px; border: 0; border-radius: 8px;
  background: transparent; color: var(--text-sidebar-section); display: flex; align-items: center;
  justify-content: space-between; font-size: 11px; line-height: 16px; font-weight: 600;
  text-transform: uppercase; letter-spacing: 0.08em;
}
.nav-section-toggle:hover { background: var(--bg-input-hover); color: var(--text-heading); }
.nav-section-toggle::after {
  content: ""; width: 7px; height: 7px; border-right: 2px solid currentColor;
  border-bottom: 2px solid currentColor; transform: rotate(45deg); margin-left: 8px;
  transition: transform 0.15s ease;
}
.nav-section.collapsed .nav-section-toggle::after { transform: rotate(-45deg); }
.nav-section-body { display: grid; gap: 6px; }
.nav-section.collapsed .nav-section-body { display: none; }
.nav-button {
  position: relative; width: 100%; text-align: left; background: transparent;
  color: var(--text-sidebar-inactive); border: 0; border-radius: 0 8px 8px 0;
  padding: 8px 14px 8px 16px; font-size: 14px; font-weight: 500; min-height: 36px;
  display: flex; align-items: center; text-decoration: none;
}
.nav-button:hover, .nav-button.active { background: var(--bg-sidebar-active); color: var(--text-sidebar-active); text-decoration: none; }
.nav-button.active::before {
  content: ""; position: absolute; left: 0; top: 6px; bottom: 6px; width: 3px;
  border-radius: 0 2px 2px 0; background: var(--border-sidebar-active);
}
.logout { margin-top: auto; }
.workspace { min-width: 0; }
.view-header {
  display: flex; align-items: flex-start; justify-content: space-between; gap: 16px;
  padding: 24px 24px 0;
}
.view-header h1 { margin: 0 0 4px; }
:deep(.panel), .workspace > :deep(section) { padding: 16px 24px 24px; }

/* Mobile top bar with the menu toggle: hidden on wide screens. */
.mobile-bar {
  display: none;
  position: sticky;
  top: 0;
  z-index: 20;
  align-items: center;
  gap: 12px;
  padding: 10px 14px;
  background: var(--sidebar);
  color: var(--sidebar-ink);
  border-bottom: 1px solid var(--line);
}
.menu-toggle {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 38px;
  height: 38px;
  padding: 0;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: transparent;
  color: var(--sidebar-ink);
  cursor: pointer;
}
.menu-toggle:hover { background: var(--bg-input-hover); }
.mobile-bar .brand-mark { line-height: 38px; }

@media (max-width: 860px) {
  .dashboard { grid-template-columns: 1fr; }
  /* The bar spans the single column above both panes. */
  .mobile-bar { display: flex; }
  .sidebar { height: auto; position: static; }
  /* Default: show content, hide the sidebar behind the toggle. */
  .sidebar { display: none; }
  /* Toggle open: reveal the menu, hide the content. */
  .dashboard.nav-open .sidebar { display: flex; }
  .dashboard.nav-open .workspace { display: none; }
}
</style>
