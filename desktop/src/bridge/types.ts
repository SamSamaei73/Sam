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
