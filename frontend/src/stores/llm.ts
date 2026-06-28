import { defineStore } from "pinia";
import { ref } from "vue";
import { api } from "@/api/client";
import type { LlmConfigIn, LlmConfigOut, LlmTestResult } from "@/api/types";

// LLM-provider settings: the user's effective config + the selectable providers. The
// API key is write-only (the server returns only `has_api_key`), so the view sends a
// key only when the user types a new one.
export const useLlmStore = defineStore("llm", () => {
  const config = ref<LlmConfigOut | null>(null);

  async function load(): Promise<void> {
    config.value = await api.get<LlmConfigOut>("/api/llm-config");
  }

  async function save(body: LlmConfigIn): Promise<void> {
    config.value = await api.put<LlmConfigOut>("/api/llm-config", body);
  }

  async function test(body: LlmConfigIn): Promise<LlmTestResult> {
    return api.post<LlmTestResult>("/api/llm-config/test", body);
  }

  return { config, load, save, test };
});
