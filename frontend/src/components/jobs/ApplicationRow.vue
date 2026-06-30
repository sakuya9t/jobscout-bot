<template>
  <article class="job-row">
    <div class="job-head">
      <div class="job-main">
        <CompanyMark :name="a.company" />
        <div>
          <div class="job-title">
            <RouterLink v-if="hasMatch" :to="`/app/positions/${a.position_id}`">{{ a.title }}</RouterLink>
            <span v-else>{{ a.title }}</span>
          </div>
          <div class="job-meta">
            {{ a.company }}<template v-if="a.location"> · {{ a.location }}</template><template v-if="applied"> · {{ applied }}</template>
          </div>
        </div>
      </div>
      <div class="job-head-right">
        <ScoreBlock v-if="showScore || a.non_matching" :score="a.match_score ?? 0" :win="a.win_probability ?? 0" :non-matching="a.non_matching" />
        <span v-else class="muted not-scored">Not scored</span>
        <div v-if="a.salary_display" class="salary">{{ a.salary_display }}</div>
      </div>
    </div>

    <span v-if="a.removed" class="pill neutral">Closed — no longer listed</span>

    <div class="job-actions">
      <MarkAppliedButton :applied="true" @toggle="emit('unmark', a.position_id)" />
      <a v-if="a.url" :href="a.url" target="_blank" rel="noopener">View posting</a>
      <span v-if="kit" class="kit-icon" :title="kit.title">{{ kit.icon }}</span>
    </div>
  </article>
</template>

<script setup lang="ts">
import { computed } from "vue";
import type { ApplicationHistoryOut } from "@/api/types";
import { fmtApplied, kitIcon } from "@/utils/format";
import CompanyMark from "@/components/CompanyMark.vue";
import ScoreBlock from "@/components/ScoreBlock.vue";
import MarkAppliedButton from "@/components/MarkAppliedButton.vue";

const props = defineProps<{ a: ApplicationHistoryOut }>();
const emit = defineEmits<{ (e: "unmark", positionId: number): void }>();

const applied = computed(() => fmtApplied(props.a.applied_at));
const kit = computed(() => kitIcon(props.a.kit_status));
// The detail page needs a stored match; without one (its match was dropped) the
// title is plain text and only the live posting link remains.
const hasMatch = computed(() => props.a.match_score !== null || props.a.non_matching);
const showScore = computed(() => props.a.match_score !== null && !props.a.non_matching);
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
.not-scored { font-size: 13px; white-space: nowrap; }
.job-actions { display: flex; align-items: center; gap: 14px; margin-top: 2px; }
.kit-icon { margin-left: auto; font-size: 15px; cursor: default; line-height: 1; }
.pill {
  display: inline-flex; align-items: center; min-height: 22px; padding: 3px 10px;
  border-radius: 999px; font-size: 12px; line-height: 16px; font-weight: 600;
  white-space: nowrap; flex-shrink: 0; width: fit-content;
}
.pill.neutral { background: var(--bg-badge-neutral); color: var(--text-secondary); }
</style>
