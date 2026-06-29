<template>
  <div class="auth-page">
    <header class="auth-top">
      <!-- Real navigation to the server-rendered "/" (landing for anon, redirects to /app
           when logged in) — a RouterLink would hit the SPA catch-all and bounce to /app/jobs. -->
      <a class="brand" href="/">JobScout</a>
      <ThemeToggle />
    </header>
    <main class="auth-main">
      <div class="card"><slot /></div>
    </main>
  </div>
</template>

<script setup lang="ts">
import ThemeToggle from "@/components/ThemeToggle.vue";
</script>

<style scoped>
.auth-page { min-height: 100vh; display: flex; flex-direction: column; }
.auth-top {
  display: flex; align-items: center; justify-content: space-between; gap: 16px;
  padding: 14px 24px; border-bottom: 1px solid var(--line);
}
.brand { font-weight: 800; font-size: 16px; color: var(--text-heading); }
.auth-main { flex: 1; display: grid; place-items: center; padding: 32px 16px; }
.card {
  width: min(100%, 400px); padding: 32px; background: var(--surface);
  border: 1px solid var(--line); border-radius: 16px; box-shadow: var(--shadow-xs);
}
</style>
