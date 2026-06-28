import { defineStore } from "pinia";
import { computed, ref } from "vue";
import { api } from "@/api/client";
import type { CompanyIn, CompanyOut, CompanyPresetOut } from "@/api/types";

// The user's watch-list companies plus the built-in presets. `availablePresets` hides
// presets already on the list (matched by name), mirroring the classic dropdown.
export const useCompaniesStore = defineStore("companies", () => {
  const companies = ref<CompanyOut[]>([]);
  const presets = ref<CompanyPresetOut[]>([]);

  const availablePresets = computed(() => {
    const watched = new Set(companies.value.map((c) => c.name.toLowerCase()));
    return presets.value.filter((p) => !watched.has(p.name.toLowerCase()));
  });

  async function load(): Promise<void> {
    companies.value = await api.get<CompanyOut[]>("/api/companies");
  }

  async function loadPresets(): Promise<void> {
    presets.value = await api.get<CompanyPresetOut[]>("/api/companies/presets");
  }

  async function add(body: CompanyIn): Promise<void> {
    await api.post("/api/companies", body);
    await load();
  }

  async function remove(id: number): Promise<void> {
    await api.del(`/api/companies/${id}`);
    await load();
  }

  return { companies, presets, availablePresets, load, loadPresets, add, remove };
});
