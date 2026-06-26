// Hand-written mirrors of the Pydantic response models in app/schemas.py. Keep these
// in sync with the backend; they're the contract the SPA's API calls rely on.

/** app/schemas.py :: UserOut */
export interface UserOut {
  id: number;
  email: string;
  telegram_chat_id: string | null;
  telegram_link_code: string | null;
}

export type KitStatus = "generating" | "ok" | "error" | null;

/** app/schemas.py :: MatchOut — one job-list row. */
export interface MatchOut {
  position_id: number;
  company: string;
  title: string;
  location: string | null;
  url: string | null;
  match_score: number;
  win_probability: number;
  reasoning: string | null;
  strengths: string[];
  gaps: string[];
  below_threshold: boolean;
  non_matching: boolean;
  removed: boolean;
  listed_at: string | null;
  salary_display: string | null;
  applied: boolean;
  kit_status: KitStatus;
}

/** app/schemas.py :: JobListRunOut — a saved snapshot for the version dropdown. */
export interface JobListRunOut {
  id: number;
  created_at: string;
  new_positions: number;
  scored: number;
  filtered: number;
  total: number;
  has_errors: boolean;
}

/** app/schemas.py :: JobListOut — one page of the job list plus run stats. */
export interface JobListOut {
  id: number | null;
  created_at: string | null;
  new_positions: number;
  scored: number;
  filtered: number;
  errors: string[];
  total: number;
  pending: number;
  llm_error: boolean;
  items: MatchOut[];
}

/** app/schemas.py :: EvaluationStatus — background scoring backlog poll. */
export interface EvaluationStatus {
  pending: number;
  in_progress: boolean;
}

/** app/schemas.py :: RunSummary — result of POST /api/run. */
export interface RunSummary {
  new_positions: number;
  scored: number;
  top_matches: MatchOut[];
  errors: string[];
  pending: number;
}

/** Subset of app/schemas.py :: CompanyOut used by the job-list company filter. */
export interface CompanyOption {
  id: number;
  name: string;
}

/** Subset of app/schemas.py :: ResumeOut used for the scan-gate. */
export interface ResumeOut {
  id: number;
  is_active: boolean;
}
