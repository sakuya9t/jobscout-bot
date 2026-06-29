<template>
  <section class="panel">
    <!-- Primary action: run / recalculate -->
    <div class="primary-actions">
      <button :disabled="!store.hasActiveResume || store.runInProgress" @click="onRun">{{ runLabel }}</button>
      <span class="muted run-status">{{ store.runStatus }}</span>
      <span v-if="!store.hasActiveResume" class="muted">Upload a resume before running a scan.</span>
    </div>

    <div class="surface">
      <!-- Toolbar -->
      <div class="toolbar">
        <div class="control grow">
          <label for="jobVersion">Saved list</label>
          <select id="jobVersion" :value="snapshotValue" @change="onSnapshot">
            <option value="latest">Latest saved list</option>
            <option v-for="r in store.runs" :key="r.id" :value="r.id">
              {{ fmtDate(r.created_at) }} · {{ r.total }} listed · {{ r.scored }} model-scored
            </option>
          </select>
        </div>
        <div class="control">
          <label for="postedWithin">Posted within</label>
          <select id="postedWithin" :value="store.postedWithin" @change="onPostedWithin">
            <option :value="1">24 hours</option>
            <option :value="3">3 days</option>
            <option :value="7">7 days</option>
            <option :value="30">30 days</option>
            <option :value="0">All time</option>
          </select>
        </div>
        <div class="control">
          <label for="jobCompany">Company</label>
          <select id="jobCompany" :value="store.companyFilter" @change="onCompany">
            <option value="">All companies</option>
            <option v-for="c in store.companyOptions" :key="c.id" :value="String(c.id)">{{ c.name }}</option>
          </select>
        </div>
        <div class="control">
          <label for="jobSort">Sort by</label>
          <select id="jobSort" :value="store.sort" @change="onSort">
            <option value="match">Best match</option>
            <option value="salary_desc">Salary: high → low</option>
            <option value="salary_asc">Salary: low → high</option>
          </select>
        </div>
        <div class="segmented" role="tablist" aria-label="Job list view">
          <button
            v-for="mode in modes"
            :key="mode.key"
            type="button"
            class="seg"
            role="tab"
            :class="{ active: store.viewMode === mode.key }"
            :aria-selected="store.viewMode === mode.key"
            @click="store.setMode(mode.key)"
          >
            {{ mode.label }}
          </button>
        </div>
      </div>

      <!-- Score/win filters (paginating modes only) -->
      <div v-if="store.paged" class="filters">
        <span class="muted">Show matches with minimum</span>
        <div class="control">
          <label for="minScore">match score</label>
          <select id="minScore" :value="store.minScore" @change="onMinScore">
            <option :value="0">0</option><option :value="50">50</option>
            <option :value="75">75</option><option :value="90">90</option>
          </select>
        </div>
        <div class="control">
          <label for="minWin">win rate</label>
          <select id="minWin" :value="store.minWin" @change="onMinWin">
            <option :value="0">0</option><option :value="50">50</option>
            <option :value="75">75</option><option :value="90">90</option>
          </select>
        </div>
      </div>

      <!-- Eval backlog banner -->
      <div v-if="evalBusy || evalPending > 0" class="eval-banner">
        ⏳ Evaluating — {{ evalPending }} position{{ evalPending === 1 ? "" : "s" }} still to score…
      </div>

      <!-- LLM error banner -->
      <div v-if="store.data?.llm_error" class="llm-banner">
        ⚠️ <b>LLM request failed</b> — some jobs couldn’t be scored. Check your provider key and model
        names, then run again. <a href="/#llm">Open LLM provider settings</a>
      </div>

      <!-- Run meta line -->
      <div v-if="store.data" class="stat-line">
        <span class="stat">{{ fmtDate(store.data.created_at) }}</span>
        <span class="stat">{{ store.data.new_positions }} new positions</span>
        <span class="stat">{{ store.data.scored }} model-scored this run</span>
        <span class="stat">{{ store.data.filtered }} screened out this run</span>
        <span class="stat">{{ store.total }} {{ store.viewMode === "all" ? "jobs" : "matches" }} listed</span>
      </div>

      <!-- Warnings -->
      <div v-if="store.data?.errors?.length" class="warning-box">
        <b>Warnings</b>
        <ul class="tight"><li v-for="(e, i) in store.data.errors" :key="i">{{ e }}</li></ul>
      </div>
    </div>

    <!-- The list -->
    <div class="job-list-shell">
      <div v-if="store.loading && !store.data" class="job-list">
        <SkeletonRow v-for="n in 3" :key="n" />
      </div>
      <div v-else-if="store.items.length" class="job-list">
        <JobRow v-for="m in store.items" :key="m.position_id" :m="m" @toggle-applied="onToggleApplied" />
      </div>
      <div v-else class="empty">{{ emptyMessage }}</div>

      <Pager
        v-if="store.paged"
        :total="store.total"
        :page-offset="store.pageOffset"
        :page-size="store.pageSize"
        @go="store.goToPage"
        @size="onPageSize"
      />
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { useJobsStore, type JobViewMode } from "@/stores/jobs";
import { usePolling } from "@/composables/usePolling";
import { fmtDate } from "@/utils/format";
import JobRow from "@/components/jobs/JobRow.vue";
import Pager from "@/components/Pager.vue";
import SkeletonRow from "@/components/SkeletonRow.vue";

const store = useJobsStore();

const modes: { key: JobViewMode; label: string }[] = [
  { key: "top5", label: "Top 5" },
  { key: "matching", label: "All matches" },
  { key: "all", label: "All jobs" },
];

const runLabel = computed(() => (store.hasSavedList ? "Recalculate matching scores" : "Run scan now"));
const snapshotValue = computed(() => (store.selectedSnapshotId === null ? "latest" : String(store.selectedSnapshotId)));

// Context-aware empty state (ported from jobListEmptyMessage()).
const emptyMessage = computed(() => {
  if (store.selectedSnapshotId !== null) return "This saved list has no positions.";
  if (store.minScore > 0 || store.minWin > 0 || store.postedWithin > 0 || store.companyFilter)
    return "No jobs match the current filters — try relaxing them.";
  return `No scored matches yet — click “${runLabel.value}” to score your current watch-list.`;
});

// ── eval-status polling (replaces pollEval) ──
const evalPending = ref(0);
const evalBusy = ref(false);
const wasBusy = ref(false);
const evalPoll = usePolling(async () => {
  try {
    const { busy, pending } = await store.pollEvaluationTick(wasBusy.value);
    evalPending.value = pending;
    evalBusy.value = busy;
    wasBusy.value = busy;
    return busy ? 4000 : null; // poll every 4s while busy; stop when idle
  } catch {
    return 6000; // transient error — keep trying
  }
});

// ── kit-status polling (replaces maybePollKits) ──
const kitPoll = usePolling(async () => {
  const generating = store.items.some((m) => m.kit_status === "generating") && store.selectedSnapshotId === null;
  if (!generating) return null;
  await store.loadJobList();
  return 5000;
});
watch(
  () => store.data,
  () => {
    const generating = store.items.some((m) => m.kit_status === "generating") && store.selectedSnapshotId === null;
    if (generating && !kitPoll.isRunning()) kitPoll.start();
  },
);

function onSnapshot(e: Event) { store.selectSnapshot((e.target as HTMLSelectElement).value); }
function onPostedWithin(e: Event) { store.postedWithin = parseInt((e.target as HTMLSelectElement).value, 10) || 0; store.applyFilterChange(); }
function onCompany(e: Event) { store.companyFilter = (e.target as HTMLSelectElement).value; store.applyFilterChange(); }
function onSort(e: Event) { store.sort = (e.target as HTMLSelectElement).value || "match"; store.applyFilterChange(); }
function onMinScore(e: Event) { store.minScore = parseInt((e.target as HTMLSelectElement).value, 10) || 0; store.applyFilterChange(); }
function onMinWin(e: Event) { store.minWin = parseInt((e.target as HTMLSelectElement).value, 10) || 0; store.applyFilterChange(); }
function onPageSize(size: number) { store.pageSize = size; store.pageOffset = 0; void store.loadJobList(); }

async function onRun() {
  await store.run();
  evalPoll.start(); // stream in matches as the background drain scores them
}

async function onToggleApplied(positionId: number, next: boolean) {
  try {
    await store.markApplied(positionId, next);
  } catch (e) {
    alert(e instanceof Error ? e.message : "Could not update application status");
  }
}

onMounted(async () => {
  // Fire the list first (primary content), the rest concurrently.
  void store.loadResumeGate();
  void store.loadCompanyOptions();
  void store.loadRuns();
  await store.loadJobList();
  evalPoll.start(); // resume the backlog indicator if a drain is already running
});
</script>

<style scoped>
.primary-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; margin-bottom: 14px; }
.run-status { text-align: left; }
.surface {
  background: var(--surface); border: 1px solid var(--line); border-radius: 16px;
  padding: 20px; box-shadow: var(--shadow-xs);
}
.toolbar { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; }
.control { display: flex; align-items: center; gap: 8px; }
.control > label { display: inline; margin: 0; font-size: 13px; color: var(--muted); white-space: nowrap; }
.control.grow { flex: 1 1 240px; }
.control.grow > select { flex: 1; min-width: 0; }
.control select, .filters select { width: auto; padding: 8px 10px; font-size: 13px; }
#jobCompany { width: 170px; max-width: 170px; text-overflow: ellipsis; }
.filters { display: flex; flex-wrap: wrap; align-items: center; gap: 14px; margin-top: 12px; }
.segmented { display: inline-flex; border: 1px solid var(--line); border-radius: 8px; overflow: hidden; min-height: 40px; }
.segmented .seg {
  background: transparent; color: var(--text-secondary); border: 0; border-left: 1px solid var(--line);
  border-radius: 0; padding: 8px 14px; font-size: 13px; cursor: pointer; min-height: 38px;
}
.segmented .seg:first-child { border-left: 0; }
.segmented .seg:hover:not(.active) { color: var(--accent); }
.segmented .seg.active { background: var(--bg-sidebar-active); color: var(--text-sidebar-active); cursor: default; }
.eval-banner, .llm-banner {
  margin-top: 12px; padding: 10px 12px; border-radius: 8px; font-size: 13px;
}
.eval-banner { border: 1px solid var(--border-default); background: var(--bg-tag); color: var(--text-secondary); }
.llm-banner { border: 1px solid var(--border-error); background: var(--bg-badge-error); color: var(--text-error); }
.stat-line { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-top: 14px; }
.stat {
  display: inline-flex; padding: 5px 9px; border-radius: 999px; background: var(--surface-soft);
  color: var(--text-secondary); font-size: 12px; line-height: 16px; font-weight: 600;
}
.warning-box {
  border: 1px solid var(--bg-badge-warning); background: var(--bg-badge-warning); color: var(--text-warning);
  border-radius: 8px; padding: 12px 14px; margin: 12px 0;
}
.job-list-shell { margin-top: 16px; }
.job-list { display: grid; gap: 16px; }
.empty {
  border: 1px dashed var(--line); border-radius: 8px; padding: 20px; color: var(--muted);
  background: var(--surface-raised);
}
</style>
