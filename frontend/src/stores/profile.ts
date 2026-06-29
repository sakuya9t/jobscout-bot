import { defineStore } from "pinia";
import { api } from "@/api/client";
import type { ApplicantProfileIn, ApplicantProfileOut } from "@/api/types";

// The reusable application profile. The view owns the editable form; this store is the
// API facade. import-from-resume returns an unsaved draft to merge in, then the user Saves.
export const useProfileStore = defineStore("profile", () => {
  function load(): Promise<ApplicantProfileOut> {
    return api.get<ApplicantProfileOut>("/api/profile");
  }

  function save(body: ApplicantProfileIn): Promise<ApplicantProfileOut> {
    return api.put<ApplicantProfileOut>("/api/profile", body);
  }

  function importFromResume(): Promise<ApplicantProfileOut> {
    return api.post<ApplicantProfileOut>("/api/profile/import-from-resume");
  }

  return { load, save, importFromResume };
});
