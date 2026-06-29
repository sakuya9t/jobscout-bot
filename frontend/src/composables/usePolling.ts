import { onUnmounted } from "vue";

// Single abstraction replacing the dashboard's hand-rolled setTimeout poll chains
// (pollEval, kit-list refresh). Uses a recursive timeout (not setInterval) so a slow
// tick can't pile up, supports a variable interval per tick, and stops automatically
// on component unmount.
//
//   const poll = usePolling(async () => {
//     const d = await api.get<EvaluationStatus>("/api/evaluation/status");
//     return d.in_progress ? 4000 : null;  // ms until next tick, or null to stop
//   });
//   poll.start();
//
// The callback returns the delay (ms) before the next run, or null/undefined to stop.

export function usePolling(tick: () => Promise<number | null | undefined>) {
  let timer: ReturnType<typeof setTimeout> | null = null;
  let running = false;

  function clear(): void {
    if (timer !== null) {
      clearTimeout(timer);
      timer = null;
    }
  }

  async function loop(): Promise<void> {
    if (!running) return;
    let next: number | null | undefined = null;
    try {
      next = await tick();
    } catch {
      next = null; // a thrown tick stops the loop; caller decides retries via the delay
    }
    if (!running) return;
    if (next && next > 0) {
      timer = setTimeout(loop, next);
    } else {
      running = false;
    }
  }

  function start(): void {
    if (running) return; // a loop is already active
    running = true;
    void loop();
  }

  function stop(): void {
    running = false;
    clear();
  }

  function isRunning(): boolean {
    return running;
  }

  onUnmounted(stop);
  return { start, stop, isRunning };
}
