// Hand-written mirrors of the Pydantic response models in app/schemas.py. Keep these
// in sync with the backend; they're the contract the SPA's API calls rely on.

/** app/schemas.py :: UserOut */
export interface UserOut {
  id: number;
  email: string;
  telegram_chat_id: string | null;
  telegram_link_code: string | null;
  // True only between logging in with a temporary password and choosing a real one.
  must_change_password: boolean;
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

/** app/schemas.py :: PositionLookupOut — resolve a pasted posting URL to a job-list row.
 *  All fields but `matched` are null when the URL isn't in the user's list. */
export interface PositionLookupOut {
  matched: boolean;
  position_id: number | null;
  company: string | null;
  title: string | null;
  location: string | null;
  applied: boolean | null;
  removed: boolean | null;
  match_score: number | null;
  win_probability: number | null;
  kit_status: KitStatus;
}

/** app/schemas.py :: LlmProviderOut / LlmConfigOut / LlmConfigIn / LlmTestResult — the
 *  LLM-provider settings view. The API key is never returned (only `has_api_key`). */
export interface LlmProviderOut {
  key: string;
  label: string;
  base_url: string;
}

export interface LlmConfigOut {
  provider: string;
  base_url: string;
  main_model: string;
  light_model: string;
  has_api_key: boolean;
  providers: LlmProviderOut[];
}

export interface LlmConfigIn {
  provider: string;
  main_model: string;
  light_model: string;
  // Omit/blank to keep the saved key; a non-empty value replaces it.
  api_key?: string | null;
}

export interface LlmModelTest {
  role: string; // "main" | "light"
  model: string;
  ok: boolean;
  detail: string;
}

export interface LlmTestResult {
  ok: boolean;
  detail: string;
  results: LlmModelTest[];
}

/** app/schemas.py :: TelegramConfigOut / TelegramConfigIn / TelegramActionResult — the
 *  Telegram delivery settings. The bot token is never returned (only `has_token`). */
export interface TelegramConfigOut {
  has_token: boolean;
  linked: boolean;
  chat_id: string | null;
  link_code: string | null;
}

export interface TelegramConfigIn {
  // Omit/blank to keep the saved token; a non-empty value replaces it.
  bot_token?: string | null;
}

export interface TelegramActionResult {
  ok: boolean;
  detail: string;
}

/** app/schemas.py :: InterestOut — one role-preference / scoring profile. */
export interface InterestOut {
  id: number;
  label: string;
  title_keywords: string | null;
  locations: string | null;
  seniority: string | null;
  employment_type: string | null;
  exclude_keywords: string | null;
  notes: string | null;
  min_score: number;
  is_active: boolean;
}

/** app/schemas.py :: InterestIn / InterestUpdate — create/update payload. */
export interface InterestIn {
  label: string;
  title_keywords?: string;
  locations?: string;
  seniority?: string;
  exclude_keywords?: string;
  notes?: string;
  min_score?: number;
}

/** app/schemas.py :: ProfileEducationOut / ProfileExperienceOut — repeating profile rows. */
export interface ProfileEducation {
  id?: number | null;
  school: string | null;
  degree: string | null;
  field_of_study: string | null;
  start_date: string | null;
  end_date: string | null;
  gpa: string | null;
  location: string | null;
  description: string | null;
}

export interface ProfileExperience {
  id?: number | null;
  company: string | null;
  title: string | null;
  location: string | null;
  start_date: string | null;
  end_date: string | null;
  is_current: boolean;
  description: string | null;
}

/** app/schemas.py :: ApplicantProfileOut / ApplicantProfileIn — the reusable application
 *  profile. All scalars optional; the three work-auth flags are tri-state (null = unset).
 *  The same shape is sent back on PUT (ids on the rows are ignored on input). */
export interface ApplicantProfileOut {
  first_name: string | null;
  last_name: string | null;
  preferred_name: string | null;
  pronouns: string | null;
  email: string | null;
  phone: string | null;
  address_line1: string | null;
  address_line2: string | null;
  city: string | null;
  state_region: string | null;
  postal_code: string | null;
  country: string | null;
  linkedin_url: string | null;
  github_url: string | null;
  portfolio_url: string | null;
  other_url: string | null;
  work_authorization: string | null;
  authorized_to_work: boolean | null;
  requires_sponsorship: boolean | null;
  open_to_relocation: boolean | null;
  desired_salary: string | null;
  salary_currency: string | null;
  remote_preference: string | null;
  preferred_locations: string | null;
  earliest_start_date: string | null;
  notice_period: string | null;
  gender: string | null;
  race_ethnicity: string | null;
  hispanic_latino: string | null;
  veteran_status: string | null;
  disability_status: string | null;
  education: ProfileEducation[];
  experience: ProfileExperience[];
}

export type ApplicantProfileIn = ApplicantProfileOut;

/** app/schemas.py :: OpenQuestionOut — one detected application question. */
export interface OpenQuestionOut {
  question: string;
  advice: string;
  suggested_answer: string;
}

/** app/schemas.py :: ApplicationKitOut — the generated (or in-progress) kit. */
export interface ApplicationKitOut {
  status: "generating" | "ok" | "error";
  looking_for: string[];
  open_questions: OpenQuestionOut[];
  cover_letter: string | null;
  revised_resume: string | null;
  resume_optimization: string | null;
  model: string | null;
  error_detail: string | null;
  updated_at: string | null;
}

/** app/schemas.py :: MatchSubScore — one aspect of the score breakdown. */
export interface MatchSubScore {
  label: string;
  score: number;
  rationale: string | null;
}

/** app/schemas.py :: PositionDetailOut — the per-position detail payload. */
export interface PositionDetailOut {
  position_id: number;
  company: string;
  title: string;
  location: string | null;
  department: string | null;
  employment_type: string | null;
  url: string | null;
  description: string | null;
  listed_at: string | null;
  match_score: number | null;
  win_probability: number | null;
  reasoning: string | null;
  strengths: string[];
  gaps: string[];
  score_breakdown: MatchSubScore[];
  non_matching: boolean;
  removed: boolean;
  applied: boolean;
  salary_min: number | null;
  salary_max: number | null;
  salary_currency: string | null;
  salary_period: string | null;
  salary_display: string | null;
  kit: ApplicationKitOut | null;
}

/** app/schemas.py :: RescoreStatusOut — per-position re-evaluation poll. */
export interface RescoreStatusOut {
  in_progress: boolean;
  error: string | null;
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

/** app/schemas.py :: CompanyPresetOut — a built-in one-click company option. */
export interface CompanyPresetOut {
  key: string;
  name: string;
  careers_url: string;
  ats_type: string;
  ats_token: string | null;
  location_hint: string | null;
}

/** app/schemas.py :: CompanyOut — a watch-list company (custom or subscribed preset). */
export interface CompanyOut {
  id: number;
  name: string;
  careers_url: string | null;
  ats_type: string;
  ats_token: string | null;
  location_hint: string | null;
  is_active: boolean;
  last_scraped_at: string | null;
  is_preset: boolean;
  requires_account: boolean;
  account_attached: boolean;
}

/** app/schemas.py :: CompanyDetailOut — CompanyOut plus the user's account state. */
export interface CompanyDetailOut extends CompanyOut {
  account_portal_url: string | null;
  account_username: string | null;
  account_has_password: boolean;
  account_notes: string | null;
}

/** app/schemas.py :: CompanyAccountIn — save portal credentials (password keep-blank). */
export interface CompanyAccountIn {
  username?: string | null;
  password?: string;
  portal_url?: string | null;
  notes?: string | null;
}

/** app/schemas.py :: CompanyIn — add a company (preset_key subscribes to a shared preset). */
export interface CompanyIn {
  name: string;
  careers_url?: string | null;
  ats_type?: string;
  ats_token?: string | null;
  location_hint?: string | null;
  preset_key?: string | null;
}

/** app/schemas.py :: ResumeOut — one uploaded resume (one per account). */
export interface ResumeOut {
  id: number;
  filename: string;
  is_active: boolean;
  created_at: string;
}

/** app/schemas.py :: ResumeContentOut — the resume's extracted plain text (preview fallback). */
export interface ResumeContentOut {
  filename: string;
  content_text: string;
}
