<template>
  <article class="job-row job-skeleton" aria-hidden="true">
    <div class="job-head">
      <div class="job-main">
        <div class="company-mark"></div>
        <div style="flex: 1; min-width: 0;">
          <div class="sk-bar sk-title"></div>
          <div class="sk-bar sk-meta"></div>
        </div>
      </div>
      <div class="sk-bar sk-score"></div>
    </div>
    <div class="sk-bar sk-line"></div>
    <div class="sk-bar sk-line short"></div>
  </article>
</template>

<style scoped>
.job-row {
  display: grid; gap: 12px; background: var(--surface); border: 1px solid var(--line);
  border-left-width: 4px; border-left-color: transparent; border-radius: 16px; padding: 20px;
  box-shadow: var(--shadow-xs);
}
.job-skeleton { pointer-events: none; }
.job-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 14px; }
.job-main { display: flex; align-items: flex-start; gap: 14px; min-width: 0; }
.sk-bar, .company-mark {
  background: linear-gradient(90deg, var(--bg-tag) 25%, var(--surface-raised) 50%, var(--bg-tag) 75%);
  background-size: 200% 100%; animation: sk-shimmer 1.4s linear infinite; border: 0; border-radius: 6px;
}
.company-mark { width: 44px; height: 44px; flex: 0 0 44px; border-radius: 10px; }
.sk-bar { height: 12px; }
.sk-title { width: 55%; height: 16px; margin-bottom: 8px; }
.sk-meta { width: 35%; }
.sk-line { width: 100%; margin-top: 10px; }
.sk-line.short { width: 80%; }
.sk-score { width: 64px; height: 40px; border-radius: 8px; }
@keyframes sk-shimmer { 0% { background-position: 200% 0; } 100% { background-position: -200% 0; } }
@media (prefers-reduced-motion: reduce) { .sk-bar, .company-mark { animation: none; } }
</style>
