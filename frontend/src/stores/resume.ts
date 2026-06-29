import { defineStore } from "pinia";
import { computed, ref } from "vue";
import { api } from "@/api/client";
import type { ResumeContentOut, ResumeOut } from "@/api/types";

// One resume per account: a new upload replaces the previous one (the list holds 0 or
// 1). `active` is the current resume used for scoring / the scan gate.
export const useResumeStore = defineStore("resume", () => {
  const resumes = ref<ResumeOut[]>([]);
  const active = computed<ResumeOut | null>(() => resumes.value[0] ?? null);

  async function load(): Promise<void> {
    resumes.value = await api.get<ResumeOut[]>("/api/resumes");
  }

  async function upload(file: File): Promise<void> {
    const fd = new FormData();
    fd.append("file", file);
    await api.post("/api/resumes", fd);
    await load();
  }

  async function remove(id: number): Promise<void> {
    await api.del(`/api/resumes/${id}`);
    await load();
  }

  function content(id: number): Promise<ResumeContentOut> {
    return api.get<ResumeContentOut>(`/api/resumes/${id}/content`);
  }

  return { resumes, active, load, upload, remove, content };
});
