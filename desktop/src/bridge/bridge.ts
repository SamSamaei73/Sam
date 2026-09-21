import type {
  ActivityResponse,
  ChallengeResponse,
  ChatResponse,
  DecisionResponse,
  EnrollBeginResponse,
  EnrollProgressResponse,
  IdentityStatus,
  ChatPrivacy,
  ProviderPreferencesInput,
  ProvidersStatus,
  LanguageChoice,
  KnowledgeIngestResponse,
  KnowledgeListResponse,
  KnowledgeQueryResponse,
  MemoryResponse,
  OperationResult,
  PermissionsResponse,
  ResourceKind,
  SpeakResponse,
  StatusResponse,
  ToolsResponse,
  VoiceResponse,
} from "./types";

/**
 * The ONLY way the UI reaches Sam. Every method is a fixed, typed operation.
 * There is deliberately no `request(url)`, `invoke(command)`, `fetch`,
 * `runShell` or `readFile`; and no method accepts a principal, scope, risk,
 * permission decision, endpoint, model, voice reference or API key.
 */
export interface SamBridge {
  status(): Promise<StatusResponse>;
  chat(message: string, language?: LanguageChoice, privacy?: ChatPrivacy): Promise<ChatResponse>;
  knowledgeList(): Promise<KnowledgeListResponse>;
  knowledgeQuery(query: string): Promise<KnowledgeQueryResponse>;
  knowledgeIngest(input: {
    name: string;
    resourceType: ResourceKind;
    contentBase64: string;
    confirmationId?: string;
  }): Promise<KnowledgeIngestResponse>;
  knowledgeRemove(
    resourceId: string,
    confirmationId?: string,
  ): Promise<OperationResult>;
  memorySearch(text?: string): Promise<MemoryResponse>;
  tools(): Promise<ToolsResponse>;
  permissions(): Promise<PermissionsResponse>;
  revokeGrant(grantId: string): Promise<OperationResult>;
  activity(): Promise<ActivityResponse>;
  decideConfirmation(
    confirmationId: string,
    approved: boolean,
    stepUp?: string,
  ): Promise<DecisionResponse>;
  voiceUtterance(
    audioBase64: string,
    confirmationId?: string,
    language?: LanguageChoice,
  ): Promise<VoiceResponse>;
  speak(input: {
    text: string;
    voiceProfile: string;
    /** Language of the text; the backend refuses a voice that can't speak it. */
    language?: "fa" | "en";
    confirmationId?: string;
  }): Promise<SpeakResponse>;

  // Owner voice identity & Guest Mode. Identity is an authentication signal
  // only: none of these can grant a permission or answer a confirmation, and
  // none accepts a principal, an "owner" flag or a capability.
  identityStatus(): Promise<IdentityStatus>;
  identityEnrollBegin(input: {
    stepUp: string;
    reEnroll: boolean;
  }): Promise<EnrollBeginResponse>;
  identityEnrollSample(
    sessionId: string,
    audioBase64: string,
  ): Promise<EnrollProgressResponse>;
  identityEnrollComplete(sessionId: string): Promise<OperationResult>;
  identityEnrollCancel(sessionId: string): Promise<OperationResult>;
  identityDelete(stepUp: string): Promise<OperationResult>;
  guestChallenge(): Promise<ChallengeResponse>;
  guestStart(input: {
    challengeId: string;
    audioBase64: string;
    stepUp: string;
    minutes: number;
  }): Promise<OperationResult>;
  guestEnd(): Promise<OperationResult>;

  // AI providers, routing and privacy. Read-only status plus the owner's own
  // preferences: nothing here can enable a paid provider or carry a credential.
  providersStatus(): Promise<ProvidersStatus>;
  setProviderPreferences(input: ProviderPreferencesInput): Promise<ProvidersStatus>;
}

/** A failure the UI may show. Contains no backend body, path or secret. */
export class BridgeError extends Error {
  readonly code: string;
  readonly referenceId: string | null;
  constructor(code: string, message: string, referenceId: string | null = null) {
    super(message);
    this.name = "BridgeError";
    this.code = code;
    this.referenceId = referenceId;
  }
}

export const SAFE_ERROR_MESSAGES: Record<string, string> = {
  unavailable: "Sam's backend isn't reachable.",
  timeout: "Sam's backend took too long to respond.",
  unauthorized: "The desktop app couldn't authenticate with Sam's backend.",
  not_configured: "The desktop bridge isn't configured.",
  step_up_unavailable:
    "Critical actions can't be approved from this window. Nothing was changed.",
  step_up_failed: "That step-up secret wasn't accepted.",
  step_up_locked: "Too many wrong attempts. The request was denied.",
  guest_mode_active: "Guest Mode is active. End Guest Mode to use this.",
  too_large: "That is too large to send.",
  invalid: "That request was not valid.",
  failed: "Something went wrong.",
};

export function toBridgeError(value: unknown): BridgeError {
  if (value instanceof BridgeError) return value;
  const raw = typeof value === "string" ? value : "";
  const code = raw in SAFE_ERROR_MESSAGES ? raw : "failed";
  return new BridgeError(code, SAFE_ERROR_MESSAGES[code] ?? "Something went wrong.");
}
