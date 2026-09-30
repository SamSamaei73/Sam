import { invoke } from "@tauri-apps/api/core";
import { toBridgeError, type SamBridge } from "./bridge";

/**
 * Production bridge. Each call is one *named* Tauri command implemented in
 * Rust with a fixed loopback destination. The webview never sees the backend
 * URL or the bridge token, and cannot choose either.
 */
async function call<T>(command: string, args?: Record<string, unknown>): Promise<T> {
  try {
    return (await invoke<T>(command, args)) as T;
  } catch (error) {
    throw toBridgeError(error);
  }
}

export const tauriBridge: SamBridge = {
  status: () => call("sam_status"),
  chat: (message, language, privacy) =>
    call("sam_chat", { message, language: language ?? "auto", privacy: privacy ?? "normal" }),
  knowledgeList: () => call("sam_knowledge_list"),
  knowledgeQuery: (query) => call("sam_knowledge_query", { query }),
  knowledgeIngest: ({ name, resourceType, contentBase64, confirmationId }) =>
    call("sam_knowledge_ingest", {
      name,
      resourceType,
      contentBase64,
      confirmationId: confirmationId ?? null,
    }),
  knowledgeRemove: (resourceId, confirmationId) =>
    call("sam_knowledge_remove", {
      resourceId,
      confirmationId: confirmationId ?? null,
    }),
  memorySearch: (text) => call("sam_memory_search", { text: text ?? null }),
  tools: () => call("sam_tools"),
  permissions: () => call("sam_permissions"),
  revokeGrant: (grantId) => call("sam_revoke_grant", { grantId }),
  activity: () => call("sam_activity"),
  decideConfirmation: (confirmationId, approved, stepUp) =>
    call("sam_decide_confirmation", { confirmationId, approved, stepUp: stepUp ?? null }),
  voiceUtterance: (audioBase64, confirmationId, language, handsFree) =>
    call("sam_voice_utterance", {
      audioBase64,
      confirmationId: confirmationId ?? null,
      language: language ?? "auto",
      handsFree: handsFree ?? false,
    }),
  voiceWake: (audioBase64) => call("sam_voice_wake", { audioBase64 }),
  setVoiceActivation: (enabled) => call("sam_voice_activation", { enabled }),
  speak: ({ text, voiceProfile, language, confirmationId }) =>
    call("sam_speak", {
      text,
      voiceProfile,
      language: language ?? null,
      confirmationId: confirmationId ?? null,
    }),
  identityStatus: () => call("sam_identity_status"),
  identityEnrollBegin: ({ stepUp, reEnroll }) =>
    call("sam_identity_enroll_begin", { stepUp, reEnroll }),
  installVoiceComponents: () => call("sam_voice_models_install"),
  ownerSetup: ({ stepUp, confirm }) => call("sam_owner_setup", { stepUp, confirm }),
  restartBackend: () => call("sam_restart_backend"),
  identityEnrollSample: (sessionId, audioBase64) =>
    call("sam_identity_enroll_sample", { sessionId, audioBase64 }),
  identityEnrollComplete: (sessionId) =>
    call("sam_identity_enroll_complete", { sessionId }),
  identityEnrollCancel: (sessionId) => call("sam_identity_enroll_cancel", { sessionId }),
  identityDelete: (stepUp) => call("sam_identity_delete", { stepUp }),
  guestChallenge: () => call("sam_guest_challenge"),
  guestStart: ({ challengeId, audioBase64, stepUp, minutes }) =>
    call("sam_guest_start", { challengeId, audioBase64, stepUp, minutes }),
  guestEnd: () => call("sam_guest_end"),
  providersStatus: () => call("sam_models_status"),
  setProviderPreferences: (input) =>
    call("sam_models_preferences", {
      preferredProvider: input.preferred_provider,
      allowFreeFallback: input.allow_free_fallback,
      personalToFreeTier: input.personal_to_free_tier,
      privateToFreeTier: input.private_to_free_tier,
      claudeImprovementState: input.claude_improvement_state,
      privateToClaudeWhenImprovementEnabled: input.private_to_claude_when_improvement_enabled,
      geminiAttestation: input.gemini_attestation,
      topicBlocklist: input.topic_blocklist,
      stepUp: input.stepUp ?? null,
    }),
  professionalProfile: () => call("sam_professional_profile"),
  professionalIngest: ({ name, sourceType, privacyClass, resourceType, contentBase64, useCandidates }) =>
    call("sam_professional_ingest", {
      name,
      sourceType,
      privacyClass,
      resourceType,
      contentBase64,
      useCandidates,
    }),
  professionalReview: (input) =>
    call("sam_professional_review", {
      action: input.action,
      claimId: input.claimId ?? null,
      conflictId: input.conflictId ?? null,
      optionId: input.optionId ?? null,
      sourceId: input.sourceId ?? null,
      privacyClass: input.privacyClass ?? null,
    }),
  professionalRemove: (sourceId, confirmationId) =>
    call("sam_professional_remove", { sourceId, confirmationId: confirmationId ?? null }),
  professionalQuery: (mode, text) => call("sam_professional_query", { mode, text }),
  proactiveOverview: () => call("sam_proactive_overview"),
  proactiveCreate: (input) =>
    call("sam_proactive_create", {
      title: input.title,
      taskType: input.taskType,
      timingMode: input.timingMode,
      action: input.action,
      schedule: input.schedule,
      conditionId: input.conditionId,
      conditionParams: input.conditionParams,
      semantics: input.semantics,
      instruction: input.instruction,
      privacyClass: input.privacyClass,
      notificationLevel: input.notificationLevel,
      proposedAction: input.proposedAction,
      cooldownHours: input.cooldownHours,
      enabled: input.enabled,
    }),
  proactiveUpdate: (input) =>
    call("sam_proactive_update", {
      taskId: input.taskId,
      enabled: input.enabled ?? null,
      title: input.title ?? null,
      schedule: input.schedule ?? null,
      instruction: input.instruction ?? null,
      privacyClass: input.privacyClass ?? null,
      notificationLevel: input.notificationLevel ?? null,
      cooldownHours: input.cooldownHours ?? null,
    }),
  proactiveDelete: (taskId, confirmationId) =>
    call("sam_proactive_delete", { taskId, confirmationId: confirmationId ?? null }),
  proactiveRun: (taskId) => call("sam_proactive_run", { taskId }),
  proactiveNotification: (notificationId, action) =>
    call("sam_proactive_notification", { notificationId, action }),
  proactiveScheduler: (enabled, remember) =>
    call("sam_proactive_scheduler", remember === undefined ? { enabled } : { enabled, remember }),
  careerOverview: () => call("sam_career_overview"),
  careerOpportunity: (input) =>
    call("sam_career_opportunity", {
      action: input.action,
      opportunityId: input.opportunityId ?? null,
      draftId: input.draftId ?? null,
      text: input.text ?? null,
      url: input.url ?? null,
      sourceKind: input.sourceKind ?? null,
      opportunityType: input.opportunityType ?? null,
      applicationUrl: input.applicationUrl ?? null,
      externalId: input.externalId ?? null,
    }),
  careerFit: (opportunityId, contactId) => call("sam_career_fit", { opportunityId, contactId: contactId ?? null }),
  careerDraft: (input) =>
    call("sam_career_draft", {
      action: input.action,
      opportunityId: input.opportunityId ?? null,
      draftId: input.draftId ?? null,
      documentId: input.documentId ?? null,
      questionId: input.questionId ?? null,
      text: input.text ?? null,
      questions: input.questions ?? [],
      lines: input.lines ?? [],
      motivation: input.motivation ?? null,
      direction: input.direction ?? null,
      confirmationId: input.confirmationId ?? null,
    }),
  careerSubmit: (draftId, confirmationId) =>
    call("sam_career_submit", { draftId, confirmationId: confirmationId ?? null }),
  careerContact: (input) =>
    call("sam_career_contact", {
      name: input.name,
      role: input.role,
      organization: input.organization,
      sourceKind: input.sourceKind,
      url: input.url,
      quote: input.quote,
      email: input.email ?? null,
      opportunityId: input.opportunityId ?? null,
      researchTopics: input.researchTopics ?? [],
    }),
  careerOutreach: (input) =>
    call("sam_career_outreach", {
      action: input.action,
      outreachId: input.outreachId ?? null,
      contactId: input.contactId ?? null,
      followUpId: input.followUpId ?? null,
      kind: input.kind ?? null,
      channel: input.channel ?? null,
      opportunityId: input.opportunityId ?? null,
      note: input.note ?? null,
    }),
  careerSend: (outreachId, confirmationId, emailConfirmationId) =>
    call("sam_career_send", {
      outreachId,
      confirmationId: confirmationId ?? null,
      emailConfirmationId: emailConfirmationId ?? null,
    }),
  careerPreferences: (input) => call("sam_career_preferences", { preferences: input }),
};
