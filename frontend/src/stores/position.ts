import { defineStore } from "pinia";
import { api } from "@/api/client";
import type { ApplicationKitOut, PositionDetailOut, RescoreStatusOut } from "@/api/types";

// API facade for the position detail page (detail payload, application kit generation +
// polling, per-position re-score + polling, mark-applied). The view owns the live state.
export const usePositionStore = defineStore("position", () => {
  function loadDetail(id: number): Promise<PositionDetailOut> {
    return api.get<PositionDetailOut>(`/api/positions/${id}/detail`);
  }
  function generateKit(id: number): Promise<ApplicationKitOut> {
    return api.post<ApplicationKitOut>(`/api/positions/${id}/kit`);
  }
  function getKit(id: number): Promise<ApplicationKitOut> {
    return api.get<ApplicationKitOut>(`/api/positions/${id}/kit`);
  }
  function rescore(id: number): Promise<RescoreStatusOut> {
    return api.post<RescoreStatusOut>(`/api/positions/${id}/rescore`);
  }
  function rescoreStatus(id: number): Promise<RescoreStatusOut> {
    return api.get<RescoreStatusOut>(`/api/positions/${id}/rescore`);
  }
  function setApplied(id: number, applied: boolean): Promise<unknown> {
    return applied ? api.post(`/api/applications/${id}`) : api.del(`/api/applications/${id}`);
  }
  return { loadDetail, generateKit, getKit, rescore, rescoreStatus, setApplied };
});
