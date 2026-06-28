import { onMounted, onUnmounted } from "vue";
import { onBeforeRouteLeave } from "vue-router";
import { useJobsStore } from "@/stores/jobs";

// The two settings views whose edits change what gets scraped/scored. Moving between
// them keeps editing (no flush); leaving the pair triggers the pending re-score.
const RESCORE_ROUTES = ["/app/companies", "/app/interests"];

/**
 * Wire a Companies/Interests view into the batched re-score: ensure the resume gate is
 * known, flush the pending scan when navigating out of the settings pair, and fire a
 * one-shot `sendBeacon` on page unload. Mirrors the classic dashboard's showView flush
 * + pagehide beacon. Call once from the view's setup.
 */
export function useRescoreOnLeave(): void {
  const jobs = useJobsStore();

  function beacon(): void {
    if (jobs.pendingRescore && jobs.hasActiveResume) {
      jobs.pendingRescore = false;
      navigator.sendBeacon("/api/run");
    }
  }

  onMounted(() => {
    void jobs.loadResumeGate(); // so flushRescore knows whether scoring can run
    window.addEventListener("pagehide", beacon);
  });
  onUnmounted(() => window.removeEventListener("pagehide", beacon));

  // Leaving the settings pair (not just hopping between Companies and Interests) fires
  // the scan. Don't await — let navigation proceed while the run kicks off in the store.
  onBeforeRouteLeave((to) => {
    if (!RESCORE_ROUTES.includes(to.path)) void jobs.flushRescore();
    return true;
  });
}
