import { defineStore } from "pinia";
import { ref } from "vue";
import { api } from "@/api/client";
import type { InterestIn, InterestOut } from "@/api/types";

// Role-preference / scoring profiles. Editing a scoring field (not just the label or
// min_score threshold) invalidates matches server-side; the view decides whether to
// mark a re-score pending — see useRescoreOnLeave.
export const useInterestsStore = defineStore("interests", () => {
  const interests = ref<InterestOut[]>([]);

  async function load(): Promise<void> {
    interests.value = await api.get<InterestOut[]>("/api/interests");
  }

  async function create(body: InterestIn): Promise<void> {
    await api.post("/api/interests", body);
    await load();
  }

  async function update(id: number, body: InterestIn): Promise<void> {
    await api.patch(`/api/interests/${id}`, body);
    await load();
  }

  async function remove(id: number): Promise<void> {
    await api.del(`/api/interests/${id}`);
    await load();
  }

  return { interests, load, create, update, remove };
});
