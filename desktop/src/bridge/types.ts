/**
 * Wire types for the desktop bridge. They mirror `sam/desktop/models.py`.
 *
 * Nothing here can express authority: there is no principal, scope, risk-to-
 * apply, permission decision or endpoint in any request type. Response types
 * are untrusted display data (model/document/provider text included).
 */

export type Capability =
  | "available"
  | "configured"
  | "not_configured"
  | "foundation_ready"
  | "unavailable";

export type OperationStatus =
  | "ok"
  | "confirmation_required"
  | "denied"
  | "rejected"
  | "failed"
  | "not_configured";

export type Risk = "low" | "medium" | "high" | "critical";

export interface Challenge {
  confirmation_id: string;
  action: string;
  resource: string;
  scope: string;
  risk: Risk;
  target: string | null;
  reason: string | null;
  expires_at: string;
}

export interface OperationResult {
  status: OperationStatus;
  reason_code: string | null;
  message: string | null;
  reference_id: string | null;
  challenge: Challenge | null;
}

export interface ChatResponse extends OperationResult {
  reply: string | null;
  language: ResponseLanguage | null;
  direction: Direction | null;
}

export interface StatusResponse {
  backend: { status: string; service: string; environment: string };
  agent: Capability;
  knowledge: Capability;
  memory: Capability;
  tools: Capability;
  tool_count: number;
  server_count: number;
  voice_input: Capability;
  speech_output: Capability;
  speech_profiles: { profile_id: string; languages: ResponseLanguage[] }[];
  voice_identity: Capability;
  persian_tts: Capability;
  computer_control: Capability;
  coding_agent: Capability;
  conversation_history: "session_local";
  memory_storage: "in_process";
  principal_label: string;
}

export interface SourceLocation {
  page_number: number | null;
  section_title: string | null;
  paragraph_index: number | null;
  character_start: number | null;
  character_end: number | null;
}

export interface ResourceInfo {
  resource_id: string;
  name: string;
  resource_type: string;
  size_bytes: number;
  chunk_count: number;
  created_at: string;
}

export interface KnowledgeListResponse extends OperationResult {
  resources: ResourceInfo[];
}

export interface KnowledgeHit {
  resource_id: string;
  resource_name: string;
  resource_type: string;
  chunk_id: string;
  score: number;
  snippet: string;
  location: SourceLocation;
}

export interface KnowledgeQueryResponse extends OperationResult {
  hits: KnowledgeHit[];
}

export interface KnowledgeIngestResponse extends OperationResult {
  resource: ResourceInfo | null;
  duplicate_of: string | null;
}

export interface MemoryItem {
  memory_id: string;
  memory_type: string;
  content: string;
  source: string;
  confidence: string;
  created_at: string;
  tags: string[];
}

export interface MemoryResponse extends OperationResult {
  items: MemoryItem[];
  working: MemoryItem[];
}

export interface ToolInfo {
  tool_id: string;
  server_id: string;
  description: string;
  permission_resource: string;
  permission_action: string;
  enabled: boolean;
  verification_required: boolean;
  credential_configured: boolean;
}

export interface ToolsResponse {
  state: Capability;
  servers: { server_id: string; display_name: string }[];
  tools: ToolInfo[];
}

export interface GrantInfo {
  grant_id: string;
  resource: string;
  action: string;
  scope: string;
  status: "active" | "revoked";
  expires_at: string | null;
  requires_confirmation: boolean;
  origin: string | null;
}

export interface PermissionsResponse {
  grants: GrantInfo[];
}

export interface ActivityItem {
  timestamp: string;
  kind: string;
  label: string;
  outcome: string;
  risk: string | null;
}

export interface ActivityResponse {
  scope: "current_session";
  items: ActivityItem[];
}

export interface VoiceResponse extends OperationResult {
  transcript: string | null;
  forwarded_to_agent: boolean;
  reply: string | null;
  /** Safe, normalized speaker metadata: never a score or biometric value. */
  speaker: "owner" | "guest" | null;
  speaker_result: string | null;
  language: ResponseLanguage | null;
  direction: Direction | null;
}

export interface SpeakResponse extends OperationResult {
  audio_base64: string | null;
  audio_format: string | null;
  byte_length: number | null;
}

export interface DecisionResponse {
  status: "approved" | "denied";
  confirmation_id: string;
}

export type LanguageChoice = "auto" | "fa" | "en";
export type ResponseLanguage = "fa" | "en";
export type Direction = "rtl" | "ltr";

export type ResourceKind = "pdf" | "txt" | "markdown" | "json" | "csv";

export interface GuestInfo {
  active: boolean;
  seconds_remaining: number;
}

/** Everything Settings may show about owner voice identity. */
export interface IdentityStatus {
  available: boolean;
  /** null: the secure store could not be read. */
  enrolled: boolean | null;
  mode: "owner_only" | "guest_mode";
  guest: GuestInfo;
  last_verification: "verified" | "not_verified" | "unknown" | null;
  speaker_model: Capability;
  local_stt: Capability;
  persian_tts: Capability;
  samples_needed: number;
  samples_max: number;
}

export interface EnrollBeginResponse extends OperationResult {
  session_id: string | null;
  samples_needed: number;
}

export interface EnrollProgressResponse extends OperationResult {
  accepted: boolean;
  sample_count: number;
  samples_needed: number;
}

export interface ChallengeResponse extends OperationResult {
  challenge_id: string | null;
  text_en: string | null;
  text_fa: string | null;
  expires_in_seconds: number;
}

// ---- AI providers, routing and privacy (Phase 13). Display/preferences only:
// no key, token, prefix, header or provider response body ever appears here.

export type ProviderId = "claude_subscription" | "gemini_free" | "openai_api" | "grok_api";
export type ProviderState =
  | "available"
  | "rate_limited"
  | "usage_limit"
  | "unauthorized"
  | "not_configured"
  | "disabled"
  | "unattested"
  | "unavailable";
export type CostClass = "subscription_included" | "free_tier" | "paid_api" | "local";
export type ClaudeImprovementState = "unknown" | "owner_reports_disabled" | "owner_reports_enabled";
export type GeminiAttestation = "unknown" | "owner_attested_unbilled";
export type ChatPrivacy = "normal" | "personal" | "private";

export interface ProviderStatus {
  provider_id: ProviderId;
  display_name: string;
  state: ProviderState;
  enabled: boolean;
  cost_class: CostClass;
  external: boolean;
  free_tier_data_use: boolean;
  note: string;
  detail?: string | null;
  models: string[];
}

export interface ProviderPreferences {
  preferred_provider: ProviderId | null;
  allow_free_fallback: boolean;
  personal_to_free_tier: boolean;
  private_to_free_tier: boolean;
  claude_improvement_state: ClaudeImprovementState;
  private_to_claude_when_improvement_enabled: boolean;
  gemini_attestation: GeminiAttestation;
}

export interface ContentPolicyInfo {
  mode: "permissive";
  topic_blocklist: string[];
  follow_user_tone: boolean;
  private_content_auto_memory: false;
}

export interface ProvidersStatus {
  available: boolean;
  providers: ProviderStatus[];
  preferences: ProviderPreferences | null;
  content: ContentPolicyInfo | null;
  routing_mode: "auto";
  paid_fallback: "off";
  max_provider_attempts: number;
}

/** What the owner may set. There is no field for an endpoint, key, model,
 * cost class or billing switch. `stepUp` is needed only to LOOSEN privacy. */
export interface ProviderPreferencesInput extends ProviderPreferences {
  topic_blocklist: string[];
  stepUp?: string;
}

// ---- Professional Intelligence (Phase 14). Display data only: every fact carries
// its provenance and a deterministic evidence strength. Nothing here can assert a
// verification, a principal or a permission; the backend computes all of it.

export type ProfessionalSourceType =
  | "master_cv"
  | "publication"
  | "transcript"
  | "project_documentation"
  | "github"
  | "linkedin_export"
  | "owner_document";
/** The owner's choice for a source. There is no "secret" (never ingested). */
export type ProfessionalPrivacy = "public" | "personal" | "private";
/** Documentary support only, counted in independent source families. */
export type EvidenceStrength = "none" | "single_source" | "corroborated";
/** The owner's decision: independent of documentary strength. */
export type ReviewState = "unreviewed" | "owner_confirmed" | "owner_rejected";
export type EvidenceNature = "explicit_source" | "inferred_relationship" | "owner_attestation" | "model_candidate";
export type Sensitivity = "public" | "normal" | "personal" | "private";
export type MatchStatus = "matched" | "partially_supported" | "not_supported" | "unknown";

export interface ProEvidence {
  evidence_id: string;
  source_id: string;
  source_label: string;
  source_type: ProfessionalSourceType;
  source_family_id: string;
  nature: EvidenceNature;
  page_number: number | null;
  section_title: string | null;
  paragraph_index: number | null;
  character_start: number | null;
  character_end: number | null;
  reference: string;
  context_claim_id: string | null;
  context_statement: string | null;
  basis_evidence_id: string | null;
}

export interface ProClaim {
  claim_id: string;
  category: string;
  statement: string;
  strength: EvidenceStrength;
  review: ReviewState;
  accepted: boolean;
  inferred: boolean;
  candidate: boolean;
  sensitivity: Sensitivity;
  attributes: Record<string, string>;
  conflicted_attributes: string[];
  source_count: number;
  source_family_count: number;
  canonical: boolean;
  evidence: ProEvidence[];
}

export interface ProPublication {
  claim_id: string;
  strength: EvidenceStrength;
  review: ReviewState;
  accepted: boolean;
  sensitivity: Sensitivity;
  title: string;
  authors: string[];
  owner_author_position: number | null;
  venue: string | null;
  year: string | null;
  doi: string | null;
  abstract: string | null;
  methods: string | null;
  models: string[];
  datasets: string | null;
  findings: string | null;
  limitations: string | null;
  topics: string[];
  owner_contribution: string | null;
  evidence: ProEvidence[];
  conflicted_attributes: string[];
}

export interface ProEducation {
  claim_id: string;
  strength: EvidenceStrength;
  review: ReviewState;
  accepted: boolean;
  sensitivity: Sensitivity;
  institution: string | null;
  degree: string;
  subject: string;
  classification: string | null;
  start: string | null;
  end: string | null;
  modules: string[];
  dissertation: string | null;
  evidence: ProEvidence[];
  conflicted_attributes: string[];
}

export interface ProTimelineEntry {
  claim_id: string;
  employer: string;
  title: string;
  start: string | null;
  end: string | null;
  status: "resolved" | "conflicted" | "incomplete";
  months: number;
  approximate: boolean;
}

export interface ProExperience {
  entries: ProTimelineEntry[];
  total_months: number;
  total_years: number;
  remainder_months: number;
  conservative_months: number;
  excluded_conflicted: number;
  excluded_incomplete: number;
  gaps: string[];
}

export interface ProConflictOption {
  option_id: string;
  value: string;
  claim_id: string | null;
  source_ids: string[];
}

export interface ProConflict {
  conflict_id: string;
  kind: "attribute" | "role_overlap";
  attribute: string;
  claim_ids: string[];
  options: ProConflictOption[];
  resolved: boolean;
  resolved_value: string | null;
}

export interface ProGap {
  kind: string;
  detail: string;
  claim_id: string | null;
  source_id: string | null;
}

export interface ProSource {
  source_id: string;
  label: string;
  source_type: ProfessionalSourceType;
  source_family_id: string;
  version: number;
  privacy_class: ProfessionalPrivacy;
  freshness: "fresh" | "aging" | "stale";
  ingested_at: string;
  refreshed_at: string;
  resource_type: string;
  accepted_claims: number;
  pending_candidates: number;
  conflicts: number;
  candidate_extraction: string;
}

export interface ProfessionalProfile extends OperationResult {
  claims: ProClaim[];
  publications: ProPublication[];
  education: ProEducation[];
  experience: ProExperience | null;
  conflicts: ProConflict[];
  gaps: ProGap[];
  sources: ProSource[];
  counts: Record<string, number>;
}

export interface ProfessionalIngestResult extends OperationResult {
  source_id: string | null;
  ingest_status: string | null;
  claims_created: number;
  claims_updated: number;
  claims_skipped_rejected: number;
  evidence_added: number;
  conflicts_open: number;
  candidate_extraction: string;
  candidates_proposed: number;
  candidates_accepted: number;
  candidates_rejected: number;
  unmapped_skills: number;
}

export interface ProRequirement {
  status: MatchStatus;
  normalized_requirement: string;
  skills: ProClaim[];
  inferred_skills: ProClaim[];
  projects: ProClaim[];
  employment: ProClaim[];
  research: ProClaim[];
  education: ProClaim[];
  unsupported_aspects: string[];
  notes: string[];
}

export interface ProfessionalQueryResult extends OperationResult {
  mode: "search" | "evidence_for" | null;
  claims: ProClaim[];
  requirement: ProRequirement | null;
}

/** The owner's review answers. There is no "verify": only confirm/reject a
 * candidate, choose among conflicting values, or set a source's privacy class. */
export interface ProfessionalReviewInput {
  action: "confirm" | "reject" | "resolve" | "set_privacy";
  claimId?: string;
  conflictId?: string;
  optionId?: string;
  sourceId?: string;
  privacyClass?: ProfessionalPrivacy;
}

// ---- Proactive Agent / Automations (Phase 15) ----

export type ProactiveTaskType = "one_time" | "recurring" | "condition_watch";
export type ProactiveTiming = "exact_schedule" | "flexible_schedule" | "condition_watch";
export type ProactiveAction = "reminder" | "summary" | "watch";
export type ProactiveFrequency = "none" | "hourly" | "daily" | "weekly";
export type ProactiveDaypart = "morning" | "afternoon" | "evening";
export type ProactiveSemantics = "becomes_true" | "on_change" | "repeat_while_true";
export type ProactiveLevel = "silent" | "notify_owner" | "requires_attention";
/** A label for what the owner might do next. The app never executes it. */
export type ProactiveProposed =
  | "none"
  | "review_in_sam"
  | "open_professional"
  | "open_model_settings"
  | "review_deadline";
export type ProactivePrivacy = "public" | "personal" | "private";
export type ProactiveStatus = "active" | "completed" | "missed" | "expired" | "invalid";

/** A deterministic schedule. The timezone is explicit and separate from the
 * local time. There is no cron string, RRULE text or command. */
export interface ProactiveScheduleInput {
  timezone: string;
  startDate: string;
  timeOfDay: string | null;
  daypart: ProactiveDaypart | null;
  frequency: ProactiveFrequency;
  interval: number;
  weekdays: number[];
  until: string | null;
  maxRuns: number | null;
}

export interface ProSchedule {
  timezone: string;
  start_date: string;
  time_of_day: string | null;
  daypart: ProactiveDaypart | null;
  frequency: ProactiveFrequency;
  interval: number;
  weekdays: number[];
  until: string | null;
  max_runs: number | null;
}

export interface ProTask {
  task_id: string;
  title: string;
  task_type: ProactiveTaskType;
  timing_mode: ProactiveTiming;
  action: ProactiveAction;
  schedule: ProSchedule;
  condition_id: string | null;
  condition_params: Record<string, string>;
  semantics: ProactiveSemantics | null;
  instruction: string;
  privacy_class: ProactivePrivacy;
  notification_level: ProactiveLevel;
  proposed_action: ProactiveProposed;
  cooldown_hours: number;
  enabled: boolean;
  status: ProactiveStatus;
  expires_at: string | null;
  created_at: string;
  next_run_at: string | null;
  last_run_at: string | null;
  last_result: string | null;
  last_failure: string | null;
  last_reason: string | null;
  running: boolean;
  version: number;
}

export interface ProNotification {
  notification_id: string;
  task_id: string;
  title: string;
  summary: string;
  created_at: string;
  reason_code: string;
  importance: "info" | "attention";
  source_label: string;
  proposed_action: ProactiveProposed;
  /** Can be stricter than the task's class: privacy only ever tightens. */
  privacy_class: ProactivePrivacy | "normal" | "secret";
  read: boolean;
}

export interface ProHistory {
  record_id: string;
  task_id: string;
  kind: string;
  trigger: string | null;
  started_at: string;
  completed_at: string | null;
  result: string | null;
  failure: string | null;
  change: string | null;
  reason_code: string | null;
  notification_created: boolean;
  provider_id: string | null;
}

export interface ProCondition {
  condition_id: string;
  label: string;
  required_params: string[];
  optional_params: string[];
}

export interface ProLimits {
  min_interval_hours: number;
  max_tasks: number;
  max_enabled_tasks: number;
  max_title_chars: number;
  max_instruction_chars: number;
}

export interface ProactiveOverview extends OperationResult {
  tasks: ProTask[];
  notifications: ProNotification[];
  history: ProHistory[];
  conditions: ProCondition[];
  limits: ProLimits | null;
  live_runs: number;
  /** Background scheduling is off by default and after every restart. */
  scheduler_enabled: boolean;
}

export interface ProactiveSchedulerResult extends OperationResult {
  scheduler_enabled: boolean;
}

export interface ProactiveTaskResult extends OperationResult {
  task: ProTask | null;
}

export interface ProactiveCreateInput {
  title: string;
  taskType: ProactiveTaskType;
  timingMode: ProactiveTiming;
  action: ProactiveAction;
  schedule: ProactiveScheduleInput;
  conditionId: string | null;
  conditionParams: Record<string, string>;
  semantics: ProactiveSemantics;
  instruction: string;
  privacyClass: ProactivePrivacy;
  notificationLevel: ProactiveLevel;
  proposedAction: ProactiveProposed;
  cooldownHours: number;
  enabled: boolean;
}

/** Trusted task management by the owner. Omitted fields stay as they are. */
export interface ProactiveUpdateInput {
  taskId: string;
  enabled?: boolean;
  title?: string;
  schedule?: ProactiveScheduleInput;
  instruction?: string;
  privacyClass?: ProactivePrivacy;
  notificationLevel?: ProactiveLevel;
  cooldownHours?: number;
}
