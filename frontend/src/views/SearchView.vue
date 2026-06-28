<template>
  <section class="panel">
    <div class="surface">
      <JobUrlLookup />
      <p class="hint muted">
        Paste a job posting URL to find it in your scored job list — see whether you've
        already applied, then open it to mark applied or generate an application kit.
        Lookup-only: it searches your existing list and never starts a new scan.
      </p>
    </div>
  </section>
</template>

<script setup lang="ts">
import JobUrlLookup from "@/components/jobs/JobUrlLookup.vue";
</script>

<style scoped>
.surface {
  background: var(--surface); border: 1px solid var(--line); border-radius: 16px;
  padding: 20px; box-shadow: var(--shadow-xs);
}
.hint { margin-top: 14px; font-size: 13px; }
</style>
