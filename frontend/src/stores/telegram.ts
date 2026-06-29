import { defineStore } from "pinia";
import { ref } from "vue";
import { api } from "@/api/client";
import type { TelegramActionResult, TelegramConfigIn, TelegramConfigOut } from "@/api/types";

// Telegram delivery settings: token (write-only) + link state. Link/Test return an
// {ok, detail} result the view shows; Save/Link/Regen reload the state afterwards.
export const useTelegramStore = defineStore("telegram", () => {
  const config = ref<TelegramConfigOut | null>(null);

  async function load(): Promise<void> {
    config.value = await api.get<TelegramConfigOut>("/api/telegram-config");
  }

  async function save(body: TelegramConfigIn): Promise<void> {
    await api.put("/api/telegram-config", body);
    await load();
  }

  async function link(): Promise<TelegramActionResult> {
    const r = await api.post<TelegramActionResult>("/api/telegram-config/link");
    await load();
    return r;
  }

  async function test(): Promise<TelegramActionResult> {
    return api.post<TelegramActionResult>("/api/telegram-config/test");
  }

  async function regenCode(): Promise<void> {
    await api.post("/api/auth/telegram-code");
    await load();
  }

  return { config, load, save, link, test, regenCode };
});
