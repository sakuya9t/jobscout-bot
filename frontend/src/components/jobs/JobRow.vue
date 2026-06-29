<template>
  <article class="job-row">
    <div class="job-head">
      <div class="job-main">
        <CompanyMark :name="m.company" />
        <div>
          <div class="job-title"><RouterLink :to="`/app/positions/${m.position_id}`">{{ m.title }}</RouterLink></div>
          <div class="job-meta">
            {{ m.company }}<template v-if="m.location"> · {{ m.location }}</template><template v-if="listed"> · {{ listed }}</template>
          </div>
        </div>
      </div>
      <div class="job-head-right">
        <ScoreBlock :score="m.match_score" :win="m.win_probability" :non-matching="m.non_matching" />
        <div v-if="m.salary_display" class="salary">{{ m.salary_display }}</div>
      </div>
    </div>

    <span v-if="m.removed" class="pill neutral">Closed — no longer listed</span>
    <span v-if="!m.non_matching && m.below_threshold" class="pill warn">below threshold</span>
    <div v-if="m.reasoning">{{ m.reasoning }}</div>
    <template v-if="m.strengths.length">
      <div class="muted">Strengths</div>
      <ul class="tight"><li v-for="(s, i) in m.strengths" :key="i">{{ s }}</li></ul>
    </template>
    <template v-if="m.gaps.length">
      <div class="muted">Watch-outs</div>
      <ul class="tight"><li v-for="(s, i) in m.gaps" :key="i">{{ s }}</li></ul>
    </template>

    <div class="job-actions">
      <MarkAppliedButton :applied="m.applied" @toggle="(next) => emit('toggleApplied', m.position_id, next)" />
      <a v-if="m.url" :href="m.url" target="_blank" rel="noopener">View posting</a>
      <span v-if="kit" class="kit-icon" :title="kit.title">{{ kit.icon }}</span>
    </div>
  </article>
</template>

<script setup lang="ts">
import { computed } from "vue";
import type { MatchOut } from "@/api/types";
import { fmtListed, kitIcon } from "@/utils/format";
import CompanyMark from "@/components/CompanyMark.vue";
import ScoreBlock from "@/components/ScoreBlock.vue";
import MarkAppliedButton from "@/components/MarkAppliedButton.vue";

const props = defineProps<{ m: MatchOut }>();
const emit = defineEmits<{ (e: "toggleApplied", positionId: number, next: boolean): void }>();

const listed = computed(() => fmtListed(props.m.listed_at));
const kit = computed(() => kitIcon(props.m.kit_status));
</script>

<style scoped>
.job-row {
  display: grid; gap: 12px; background: var(--surface); border: 1px solid var(--line);
  border-left-width: 4px; border-left-color: transparent; border-radius: 16px; padding: 20px;
  box-shadow: var(--shadow-xs); transition: box-shadow 150ms ease, border-color 150ms ease;
}
.job-row:hover { border-left-color: var(--brand-primary); box-shadow: var(--shadow-md); }
.job-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 14px; }
.job-main { display: flex; align-items: flex-start; gap: 14px; min-width: 0; }
.job-title { font-weight: 700; font-size: 16px; line-height: 24px; }
.job-title a { color: var(--ink); text-decoration: none; }
.job-title a:hover { color: var(--accent); text-decoration: underline; }
.job-meta { color: var(--muted); font-size: 13px; }
.job-head-right { display: flex; flex-direction: column; align-items: flex-end; gap: 6px; flex-shrink: 0; }
.salary { color: var(--ink); font-size: 13px; font-weight: 650; white-space: nowrap; }
.job-actions { display: flex; align-items: center; gap: 14px; margin-top: 2px; }
.kit-icon { margin-left: auto; font-size: 15px; cursor: default; line-height: 1; }
.pill {
  display: inline-flex; align-items: center; min-height: 22px; padding: 3px 10px;
  border-radius: 999px; font-size: 12px; line-height: 16px; font-weight: 600;
  white-space: nowrap; flex-shrink: 0; width: fit-content;
}
.pill.warn { background: var(--bg-badge-warning); color: var(--text-warning); }
.pill.neutral { background: var(--bg-badge-neutral); color: var(--text-secondary); }
</style>
