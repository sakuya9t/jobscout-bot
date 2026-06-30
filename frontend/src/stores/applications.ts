import { defineStore } from "pinia";
import { computed, ref } from "vue";
import { api } from "@/api/client";
import type { ApplicationHistoryOut, ApplicationHistoryPageOut } from "@/api/types";

// The application-history view's state: one server-paginated page of the user's applied
// positions (newest first, from GET /api/applications/history) plus the actions to load
// a page, change page/size, and unmark one. Kept separate from the jobs store, which is
// interest/match-centric; this list is application-centric and includes postings that no
// longer match. Pagination mirrors the job list.
export const useApplicationsStore = defineStore("applications", () => {
  const items = ref<ApplicationHistoryOut[]>([]);
  const total = ref(0);
  const pageSize = ref(10);
  const pageOffset = ref(0);
  const loading = ref(false);
  const loaded = ref(false); // distinguishes "not fetched yet" from "fetched, empty"
  const error = ref("");

  const count = computed(() => items.value.length);

  async function load(): Promise<void> {
    loading.value = true;
    error.value = "";
    try {
      let page = await fetchPage();
      // A deletion can empty the last page (e.g. unmarking its only row) — step back to
      // the last page that still has rows so the view never shows a blank page.
      if (page.items.length === 0 && pageOffset.value > 0 && page.total > 0) {
        pageOffset.value = Math.max(0, (Math.ceil(page.total / pageSize.value) - 1) * pageSize.value);
        page = await fetchPage();
      }
      items.value = page.items;
      total.value = page.total;
      loaded.value = true;
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Could not load your application history";
    } finally {
      loading.value = false;
    }
  }

  function fetchPage(): Promise<ApplicationHistoryPageOut> {
    return api.get<ApplicationHistoryPageOut>(
      `/api/applications/history?limit=${pageSize.value}&offset=${pageOffset.value}`,
    );
  }

  function goToPage(n: number): void {
    pageOffset.value = Math.max(0, (n - 1) * pageSize.value);
    void load();
  }

  function setPageSize(size: number): void {
    pageSize.value = size;
    pageOffset.value = 0;
    void load();
  }

  // Undo "applied" for one position: drop it from the page optimistically (reverting in
  // place on failure), then reload so the freed slot refills from the next page instead
  // of the page just shrinking.
  async function unmark(positionId: number): Promise<void> {
    const idx = items.value.findIndex((a) => a.position_id === positionId);
    if (idx === -1) return;
    const [removed] = items.value.splice(idx, 1);
    total.value = Math.max(0, total.value - 1);
    try {
      await api.del(`/api/applications/${positionId}`);
    } catch {
      items.value.splice(idx, 0, removed); // revert
      total.value += 1;
      throw new Error("Could not update application status");
    }
    await load();
  }

  return {
    items, total, pageSize, pageOffset, loading, loaded, error, count,
    load, goToPage, setPageSize, unmark,
  };
});
