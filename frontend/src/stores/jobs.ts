import { defineStore } from "pinia";
import { computed, ref } from "vue";
import { api, ApiError } from "@/api/client";
import type {
  CompanyOption,
  EvaluationStatus,
  JobListOut,
  JobListRunOut,
  ResumeOut,
  RunSummary,
} from "@/api/types";

export type JobViewMode = "top5" | "matching" | "all";
export const JOB_MODES: JobViewMode[] = ["top5", "matching", "all"];

// Holds the job-list state that the Jinja dashboard kept as module globals, plus the
// data-loading actions. Query-string construction mirrors loadJobList() in
// app/templates/dashboard.html exactly (top5 is a fixed 5-row glance with the
// score/win filters forced off; the two "all" modes paginate and filter).
export const useJobsStore = defineStore("jobs", () => {
  // ── filter / mode / pagination state ──
  const viewMode = ref<JobViewMode>("top5");
  const pageSize = ref(10);
  const pageOffset = ref(0);
  const minScore = ref(0);
  const minWin = ref(0);
  const postedWithin = ref(7); // matches the "7 days" default selected in the toolbar
  const companyFilter = ref(""); // watch-list company id as string; "" = all
  const sort = ref("match");
  const selectedSnapshotId = ref<number | null>(null);

  // ── loaded data ──
  const data = ref<JobListOut | null>(null);
  const loading = ref(false);
  const runs = ref<JobListRunOut[]>([]);
  const companyOptions = ref<CompanyOption[]>([]);

  // ── run / scan-gate state ──
  const runInProgress = ref(false);
  const hasActiveResume = ref(false);
  const runStatus = ref("");

  const items = computed(() => data.value?.items ?? []);
  const total = computed(() => data.value?.total ?? 0);
  const paged = computed(() => viewMode.value !== "top5"); // the two "all" modes paginate
  const hasSavedList = computed(() => runs.value.length > 0);

  function buildQuery(): string {
    const limit = viewMode.value === "top5" ? 5 : pageSize.value;
    const offset = viewMode.value === "top5" ? 0 : pageOffset.value;
    const category = viewMode.value === "all" ? "all" : "matching";
    // The score/win filters are hidden in top-5 mode, so don't apply them there. The
    // post-date filter, however, applies in every mode.
    const ms = viewMode.value === "top5" ? 0 : minScore.value;
    const mw = viewMode.value === "top5" ? 0 : minWin.value;
    let q = `limit=${limit}&offset=${offset}&category=${category}&min_score=${ms}&min_win=${mw}`;
    if (postedWithin.value > 0) q += `&posted_within_days=${postedWithin.value}`;
    if (companyFilter.value) q += `&company_id=${encodeURIComponent(companyFilter.value)}`;
    if (sort.value !== "match") q += `&sort=${sort.value}`;
    return q;
  }

  async function loadJobList(): Promise<void> {
    const base = selectedSnapshotId.value
      ? `/api/job-lists/${selectedSnapshotId.value}`
      : "/api/job-lists/latest";
    loading.value = true;
    try {
      data.value = await api.get<JobListOut>(`${base}?${buildQuery()}`);
    } finally {
      loading.value = false;
    }
  }

  async function loadRuns(): Promise<void> {
    try {
      runs.value = await api.get<JobListRunOut[]>("/api/job-lists/runs");
    } catch {
      runs.value = [];
    }
  }

  async function loadCompanyOptions(): Promise<void> {
    try {
      const list = await api.get<CompanyOption[]>("/api/companies");
      companyOptions.value = list;
      // Reset to "all" if the selected company is gone (mirrors loadJobCompanyOptions).
      if (companyFilter.value && !list.some((c) => String(c.id) === companyFilter.value)) {
        companyFilter.value = "";
      }
    } catch {
      /* leave options as-is */
    }
  }

  async function loadResumeGate(): Promise<void> {
    try {
      const list = await api.get<ResumeOut[]>("/api/resumes");
      hasActiveResume.value = list.some((r) => r.is_active);
    } catch {
      hasActiveResume.value = false;
    }
  }

  // Mode / filter setters all reset the page offset (a new view/ordering invalidates
  // the current page), then reload.
  function setMode(mode: JobViewMode): void {
    if (!JOB_MODES.includes(mode) || mode === viewMode.value) return;
    viewMode.value = mode;
    pageOffset.value = 0;
    void loadJobList();
  }

  function applyFilterChange(): void {
    pageOffset.value = 0;
    void loadJobList();
  }

  function goToPage(n: number): void {
    pageOffset.value = (n - 1) * pageSize.value;
    void loadJobList();
  }

  function selectSnapshot(value: string): void {
    selectedSnapshotId.value = value === "latest" ? null : parseInt(value, 10);
    pageOffset.value = 0;
    void loadJobList();
  }

  async function run(): Promise<void> {
    if (!hasActiveResume.value || runInProgress.value) return;
    runInProgress.value = true;
    runStatus.value = "Scanning company sites…";
    try {
      const d = await api.post<RunSummary>("/api/run");
      runStatus.value =
        d.pending > 0
          ? `${d.new_positions} new positions · evaluating ${d.pending} in the background…`
          : `${d.new_positions} new positions · up to date`;
      selectedSnapshotId.value = null;
      pageOffset.value = 0;
      await loadRuns();
      await loadJobList();
    } catch (e) {
      runStatus.value = e instanceof ApiError ? e.message : "Run failed";
    } finally {
      runInProgress.value = false;
    }
  }

  // Toggle a position's applied status in place (no list reload, so paging/scroll are
  // preserved). Optimistic: flips locally, reverts on failure.
  async function markApplied(positionId: number, applied: boolean): Promise<void> {
    const row = items.value.find((m) => m.position_id === positionId);
    if (row) row.applied = applied;
    try {
      if (applied) await api.post(`/api/applications/${positionId}`);
      else await api.del(`/api/applications/${positionId}`);
    } catch {
      if (row) row.applied = !applied; // revert
      throw new Error("Could not update application status");
    }
  }

  /** One eval-status poll tick. Returns the ms until the next tick, or null to stop.
   *  Reloads the live list as matches get scored, only when viewing "latest" (the
   *  busy→idle transition also refreshes the saved-runs dropdown). */
  async function pollEvaluationTick(wasBusy: boolean): Promise<{ busy: boolean; pending: number }> {
    const d = await api.get<EvaluationStatus>("/api/evaluation/status");
    const busy = d.in_progress || d.pending > 0;
    if (busy && selectedSnapshotId.value === null) await loadJobList();
    if (!busy && wasBusy) {
      await loadRuns();
      if (selectedSnapshotId.value === null) await loadJobList();
    }
    return { busy, pending: d.pending };
  }

  return {
    viewMode, pageSize, pageOffset, minScore, minWin, postedWithin, companyFilter, sort,
    selectedSnapshotId, data, loading, runs, companyOptions, runInProgress, hasActiveResume,
    runStatus, items, total, paged, hasSavedList,
    loadJobList, loadRuns, loadCompanyOptions, loadResumeGate, setMode, applyFilterChange,
    goToPage, selectSnapshot, run, markApplied, pollEvaluationTick,
  };
});
