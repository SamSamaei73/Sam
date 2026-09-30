//! Sam desktop shell.
//!
//! The webview can call exactly the commands below. Each is a fixed, typed
//! operation with a bounded, validated argument list, mapped to one fixed
//! backend route. There is no command that takes a URL, path, header, method,
//! shell command or arbitrary JSON, and no argument for a principal, scope,
//! risk, permission decision, endpoint, model, voice reference or key.
//!
//! This layer is not an authorization boundary: the backend's
//! PermissionEngine is the sole authority.

mod backend;
#[cfg(test)]
mod config_tests;
mod sidecar;
mod validate;

use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};

use backend::{Backend, BackendSlot, BridgeError, Route, Slot};
use serde::Deserialize;
use serde_json::{json, Value};
use tauri::State;

pub struct AppState {
    backend: Arc<BackendSlot>,
}

type CommandResult = Result<Value, String>;

fn run(state: &State<'_, AppState>, route: Route, body: Option<Value>) -> CommandResult {
    state
        .backend
        .call(route, body)
        .map_err(|error: BridgeError| error.as_code().to_string())
}

fn invalid<T>(result: Result<T, BridgeError>) -> Result<T, String> {
    result.map_err(|error| error.as_code().to_string())
}

#[tauri::command]
fn sam_status(state: State<'_, AppState>) -> CommandResult {
    let mut status = run(&state, Route::Status, None)?;
    // Which shell is running, so a stale or development build is obvious in
    // About. A fixed label only: no path, port, token or environment value.
    if let Some(map) = status.as_object_mut() {
        let build = if cfg!(debug_assertions) {
            "development"
        } else {
            "release"
        };
        map.insert("desktop_build".to_string(), json!(build));
    }
    Ok(status)
}

#[tauri::command]
fn sam_chat(
    state: State<'_, AppState>,
    message: String,
    language: Option<String>,
    privacy: Option<String>,
) -> CommandResult {
    invalid(validate::text(&message, validate::MAX_MESSAGE_CHARS))?;
    let language = language.unwrap_or_else(|| "auto".to_string());
    invalid(validate::language(&language))?;
    let privacy = privacy.unwrap_or_else(|| "normal".to_string());
    invalid(validate::privacy_label(&privacy))?;
    run(
        &state,
        Route::Chat,
        Some(json!({ "message": message, "language": language, "privacy": privacy })),
    )
}

#[tauri::command]
fn sam_knowledge_list(state: State<'_, AppState>) -> CommandResult {
    run(&state, Route::KnowledgeList, None)
}

#[tauri::command]
fn sam_knowledge_query(state: State<'_, AppState>, query: String) -> CommandResult {
    invalid(validate::text(&query, validate::MAX_QUERY_CHARS))?;
    run(
        &state,
        Route::KnowledgeQuery,
        Some(json!({ "query": query })),
    )
}

#[tauri::command]
fn sam_knowledge_ingest(
    state: State<'_, AppState>,
    name: String,
    resource_type: String,
    content_base64: String,
    confirmation_id: Option<String>,
) -> CommandResult {
    invalid(validate::text(&name, validate::MAX_NAME_CHARS))?;
    invalid(validate::resource_type(&resource_type))?;
    invalid(validate::base64_payload(
        &content_base64,
        validate::MAX_DOCUMENT_BASE64,
    ))?;
    invalid(validate::optional_id(confirmation_id.as_deref()))?;
    run(
        &state,
        Route::KnowledgeIngest,
        Some(json!({
            "name": name,
            "resource_type": resource_type,
            "content_base64": content_base64,
            "confirmation_id": confirmation_id,
        })),
    )
}

#[tauri::command]
fn sam_knowledge_remove(
    state: State<'_, AppState>,
    resource_id: String,
    confirmation_id: Option<String>,
) -> CommandResult {
    invalid(validate::id(&resource_id))?;
    invalid(validate::optional_id(confirmation_id.as_deref()))?;
    run(
        &state,
        Route::KnowledgeRemove,
        Some(json!({ "resource_id": resource_id, "confirmation_id": confirmation_id })),
    )
}

#[tauri::command]
fn sam_memory_search(state: State<'_, AppState>, text: Option<String>) -> CommandResult {
    if let Some(value) = text.as_deref() {
        invalid(validate::text_allow_empty(value, validate::MAX_QUERY_CHARS))?;
    }
    run(&state, Route::MemorySearch, Some(json!({ "text": text })))
}

#[tauri::command]
fn sam_tools(state: State<'_, AppState>) -> CommandResult {
    run(&state, Route::Tools, None)
}

#[tauri::command]
fn sam_permissions(state: State<'_, AppState>) -> CommandResult {
    run(&state, Route::Permissions, None)
}

#[tauri::command]
fn sam_revoke_grant(state: State<'_, AppState>, grant_id: String) -> CommandResult {
    invalid(validate::id(&grant_id))?;
    run(
        &state,
        Route::RevokeGrant,
        Some(json!({ "grant_id": grant_id })),
    )
}

#[tauri::command]
fn sam_activity(state: State<'_, AppState>) -> CommandResult {
    run(&state, Route::Activity, None)
}

#[tauri::command]
fn sam_decide_confirmation(
    state: State<'_, AppState>,
    confirmation_id: String,
    approved: bool,
    step_up: Option<String>,
) -> CommandResult {
    invalid(validate::id(&confirmation_id))?;
    invalid(validate::optional_step_up(step_up.as_deref()))?;
    run(
        &state,
        Route::DecideConfirmation,
        Some(json!({
            "confirmation_id": confirmation_id,
            "approved": approved,
            "step_up": step_up,
        })),
    )
}

#[tauri::command]
fn sam_voice_utterance(
    state: State<'_, AppState>,
    audio_base64: String,
    confirmation_id: Option<String>,
    language: Option<String>,
    hands_free: Option<bool>,
) -> CommandResult {
    invalid(validate::base64_payload(
        &audio_base64,
        validate::MAX_AUDIO_BASE64,
    ))?;
    invalid(validate::optional_id(confirmation_id.as_deref()))?;
    let language = language.unwrap_or_else(|| "auto".to_string());
    invalid(validate::language(&language))?;
    run(
        &state,
        Route::VoiceUtterance,
        Some(json!({
            "audio_base64": audio_base64,
            "confirmation_id": confirmation_id,
            "language": language,
            "hands_free": hands_free.unwrap_or(false),
        })),
    )
}

/// One short speech segment: was it Sam's name? The backend answers with one
/// bit (never the recognized text), using its LOCAL recognizer only.
#[tauri::command]
fn sam_voice_wake(state: State<'_, AppState>, audio_base64: String) -> CommandResult {
    invalid(validate::base64_payload(
        &audio_base64,
        validate::MAX_WAKE_BASE64,
    ))?;
    run(
        &state,
        Route::VoiceWake,
        Some(json!({ "audio_base64": audio_base64 })),
    )
}

/// The owner-only hands-free switch (the backend refuses it in Guest Mode).
#[tauri::command]
fn sam_voice_activation(state: State<'_, AppState>, enabled: bool) -> CommandResult {
    run(
        &state,
        Route::VoiceActivation,
        Some(json!({ "enabled": enabled })),
    )
}

#[tauri::command]
fn sam_speak(
    state: State<'_, AppState>,
    text: String,
    voice_profile: String,
    confirmation_id: Option<String>,
    language: Option<String>,
) -> CommandResult {
    invalid(validate::text(&text, validate::MAX_SPEAK_CHARS))?;
    invalid(validate::profile_id(&voice_profile))?;
    invalid(validate::speech_language(language.as_deref()))?;
    invalid(validate::optional_id(confirmation_id.as_deref()))?;
    run(
        &state,
        Route::Speak,
        Some(json!({
            "text": text,
            "voice_profile": voice_profile,
            "confirmation_id": confirmation_id,
            "language": language,
        })),
    )
}

#[tauri::command]
fn sam_identity_status(state: State<'_, AppState>) -> CommandResult {
    run(&state, Route::IdentityStatus, None)
}

#[tauri::command]
fn sam_identity_enroll_begin(
    state: State<'_, AppState>,
    step_up: String,
    re_enroll: bool,
) -> CommandResult {
    invalid(validate::required_step_up(&step_up))?;
    run(
        &state,
        Route::IdentityEnrollBegin,
        Some(json!({ "step_up": step_up, "re_enroll": re_enroll })),
    )
}

#[tauri::command]
fn sam_identity_enroll_sample(
    state: State<'_, AppState>,
    session_id: String,
    audio_base64: String,
) -> CommandResult {
    invalid(validate::id(&session_id))?;
    invalid(validate::base64_payload(
        &audio_base64,
        validate::MAX_AUDIO_BASE64,
    ))?;
    run(
        &state,
        Route::IdentityEnrollSample,
        Some(json!({ "session_id": session_id, "audio_base64": audio_base64 })),
    )
}

#[tauri::command]
fn sam_identity_enroll_complete(state: State<'_, AppState>, session_id: String) -> CommandResult {
    invalid(validate::id(&session_id))?;
    run(
        &state,
        Route::IdentityEnrollComplete,
        Some(json!({ "session_id": session_id })),
    )
}

#[tauri::command]
fn sam_identity_enroll_cancel(state: State<'_, AppState>, session_id: String) -> CommandResult {
    invalid(validate::id(&session_id))?;
    run(
        &state,
        Route::IdentityEnrollCancel,
        Some(json!({ "session_id": session_id })),
    )
}

#[tauri::command]
fn sam_identity_delete(state: State<'_, AppState>, step_up: String) -> CommandResult {
    invalid(validate::required_step_up(&step_up))?;
    run(
        &state,
        Route::IdentityDelete,
        Some(json!({ "step_up": step_up })),
    )
}

#[tauri::command]
fn sam_guest_challenge(state: State<'_, AppState>) -> CommandResult {
    run(&state, Route::GuestChallenge, Some(json!({})))
}

#[tauri::command]
fn sam_guest_start(
    state: State<'_, AppState>,
    challenge_id: String,
    audio_base64: String,
    step_up: String,
    minutes: u32,
) -> CommandResult {
    invalid(validate::id(&challenge_id))?;
    invalid(validate::base64_payload(
        &audio_base64,
        validate::MAX_AUDIO_BASE64,
    ))?;
    invalid(validate::required_step_up(&step_up))?;
    invalid(validate::guest_minutes(minutes))?;
    run(
        &state,
        Route::GuestStart,
        Some(json!({
            "challenge_id": challenge_id,
            "audio_base64": audio_base64,
            "step_up": step_up,
            "minutes": minutes,
        })),
    )
}

#[tauri::command]
fn sam_guest_end(state: State<'_, AppState>) -> CommandResult {
    run(&state, Route::GuestEnd, Some(json!({})))
}

#[tauri::command]
fn sam_professional_profile(state: State<'_, AppState>) -> CommandResult {
    run(&state, Route::ProfessionalProfile, None)
}

#[tauri::command]
fn sam_professional_ingest(
    state: State<'_, AppState>,
    name: String,
    source_type: String,
    privacy_class: String,
    resource_type: String,
    content_base64: String,
    use_candidates: bool,
) -> CommandResult {
    invalid(validate::text(&name, validate::MAX_NAME_CHARS))?;
    invalid(validate::professional_source_type(&source_type))?;
    invalid(validate::professional_privacy(&privacy_class))?;
    invalid(validate::resource_type(&resource_type))?;
    invalid(validate::base64_payload(
        &content_base64,
        validate::MAX_DOCUMENT_BASE64,
    ))?;
    run(
        &state,
        Route::ProfessionalIngest,
        Some(json!({
            "name": name,
            "source_type": source_type,
            "privacy_class": privacy_class,
            "resource_type": resource_type,
            "content_base64": content_base64,
            "use_candidates": use_candidates,
        })),
    )
}

#[tauri::command]
fn sam_professional_review(
    state: State<'_, AppState>,
    action: String,
    claim_id: Option<String>,
    conflict_id: Option<String>,
    option_id: Option<String>,
    source_id: Option<String>,
    privacy_class: Option<String>,
) -> CommandResult {
    invalid(validate::professional_review_action(&action))?;
    invalid(validate::optional_id(claim_id.as_deref()))?;
    invalid(validate::optional_id(conflict_id.as_deref()))?;
    invalid(validate::optional_id(option_id.as_deref()))?;
    invalid(validate::optional_id(source_id.as_deref()))?;
    invalid(validate::optional_professional_privacy(
        privacy_class.as_deref(),
    ))?;
    run(
        &state,
        Route::ProfessionalReview,
        Some(json!({
            "action": action,
            "claim_id": claim_id,
            "conflict_id": conflict_id,
            "option_id": option_id,
            "source_id": source_id,
            "privacy_class": privacy_class,
        })),
    )
}

#[tauri::command]
fn sam_professional_remove(
    state: State<'_, AppState>,
    source_id: String,
    confirmation_id: Option<String>,
) -> CommandResult {
    invalid(validate::id(&source_id))?;
    invalid(validate::optional_id(confirmation_id.as_deref()))?;
    run(
        &state,
        Route::ProfessionalRemove,
        Some(json!({ "source_id": source_id, "confirmation_id": confirmation_id })),
    )
}

#[tauri::command]
fn sam_professional_query(state: State<'_, AppState>, mode: String, text: String) -> CommandResult {
    invalid(validate::professional_query_mode(&mode))?;
    invalid(validate::text(
        &text,
        validate::MAX_PROFESSIONAL_QUERY_CHARS,
    ))?;
    run(
        &state,
        Route::ProfessionalQuery,
        Some(json!({ "mode": mode, "text": text })),
    )
}

/// A proactive schedule: typed, closed and bounded. Unknown fields (a cron
/// string, an RRULE, a command) are refused by deserialization.
#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct ScheduleArgs {
    timezone: String,
    start_date: String,
    time_of_day: Option<String>,
    daypart: Option<String>,
    frequency: String,
    interval: u32,
    weekdays: Vec<u8>,
    until: Option<String>,
    max_runs: Option<u32>,
}

fn schedule_json(schedule: &ScheduleArgs) -> Result<Value, String> {
    invalid(validate::timezone(&schedule.timezone))?;
    invalid(validate::iso_date(&schedule.start_date))?;
    invalid(validate::optional_time_of_day(
        schedule.time_of_day.as_deref(),
    ))?;
    invalid(validate::optional_daypart(schedule.daypart.as_deref()))?;
    invalid(validate::proactive_frequency(&schedule.frequency))?;
    invalid(validate::bounded_number(schedule.interval, 1, 365))?;
    invalid(validate::weekdays(&schedule.weekdays))?;
    invalid(validate::optional_iso_date(schedule.until.as_deref()))?;
    if let Some(max_runs) = schedule.max_runs {
        invalid(validate::bounded_number(max_runs, 1, 1_000))?;
    }
    Ok(json!({
        "timezone": schedule.timezone,
        "start_date": schedule.start_date,
        "time_of_day": schedule.time_of_day,
        "daypart": schedule.daypart,
        "frequency": schedule.frequency,
        "interval": schedule.interval,
        "weekdays": schedule.weekdays,
        "until": schedule.until,
        "max_runs": schedule.max_runs,
    }))
}

#[tauri::command]
fn sam_proactive_overview(state: State<'_, AppState>) -> CommandResult {
    run(&state, Route::ProactiveOverview, None)
}

#[allow(clippy::too_many_arguments)]
#[tauri::command]
fn sam_proactive_create(
    state: State<'_, AppState>,
    title: String,
    task_type: String,
    timing_mode: String,
    action: String,
    schedule: ScheduleArgs,
    condition_id: Option<String>,
    condition_params: BTreeMap<String, String>,
    semantics: String,
    instruction: String,
    privacy_class: String,
    notification_level: String,
    proposed_action: String,
    cooldown_hours: u32,
    enabled: bool,
) -> CommandResult {
    invalid(validate::text(&title, validate::MAX_TASK_TITLE_CHARS))?;
    invalid(validate::proactive_task_type(&task_type))?;
    invalid(validate::proactive_timing(&timing_mode))?;
    invalid(validate::proactive_action(&action))?;
    let schedule = schedule_json(&schedule)?;
    invalid(validate::optional_condition_id(condition_id.as_deref()))?;
    invalid(validate::condition_params(&condition_params))?;
    invalid(validate::proactive_semantics(&semantics))?;
    invalid(validate::text_allow_empty(
        &instruction,
        validate::MAX_TASK_INSTRUCTION_CHARS,
    ))?;
    invalid(validate::professional_privacy(&privacy_class))?;
    invalid(validate::proactive_level(&notification_level))?;
    invalid(validate::proactive_proposed(&proposed_action))?;
    invalid(validate::bounded_number(cooldown_hours, 1, 720))?;
    run(
        &state,
        Route::ProactiveCreate,
        Some(json!({
            "title": title,
            "task_type": task_type,
            "timing_mode": timing_mode,
            "action": action,
            "schedule": schedule,
            "condition_id": condition_id,
            "condition_params": condition_params,
            "semantics": semantics,
            "instruction": instruction,
            "privacy_class": privacy_class,
            "notification_level": notification_level,
            "proposed_action": proposed_action,
            "cooldown_hours": cooldown_hours,
            "enabled": enabled,
        })),
    )
}

#[allow(clippy::too_many_arguments)]
#[tauri::command]
fn sam_proactive_update(
    state: State<'_, AppState>,
    task_id: String,
    enabled: Option<bool>,
    title: Option<String>,
    schedule: Option<ScheduleArgs>,
    instruction: Option<String>,
    privacy_class: Option<String>,
    notification_level: Option<String>,
    cooldown_hours: Option<u32>,
) -> CommandResult {
    invalid(validate::id(&task_id))?;
    if let Some(title) = &title {
        invalid(validate::text(title, validate::MAX_TASK_TITLE_CHARS))?;
    }
    let schedule = match &schedule {
        Some(schedule) => Some(schedule_json(schedule)?),
        None => None,
    };
    if let Some(instruction) = &instruction {
        invalid(validate::text_allow_empty(
            instruction,
            validate::MAX_TASK_INSTRUCTION_CHARS,
        ))?;
    }
    invalid(validate::optional_professional_privacy(
        privacy_class.as_deref(),
    ))?;
    invalid(validate::optional_proactive_level(
        notification_level.as_deref(),
    ))?;
    if let Some(hours) = cooldown_hours {
        invalid(validate::bounded_number(hours, 1, 720))?;
    }
    run(
        &state,
        Route::ProactiveUpdate,
        Some(json!({
            "task_id": task_id,
            "enabled": enabled,
            "title": title,
            "schedule": schedule,
            "instruction": instruction,
            "privacy_class": privacy_class,
            "notification_level": notification_level,
            "cooldown_hours": cooldown_hours,
        })),
    )
}

#[tauri::command]
fn sam_proactive_delete(
    state: State<'_, AppState>,
    task_id: String,
    confirmation_id: Option<String>,
) -> CommandResult {
    invalid(validate::id(&task_id))?;
    invalid(validate::optional_id(confirmation_id.as_deref()))?;
    run(
        &state,
        Route::ProactiveDelete,
        Some(json!({ "task_id": task_id, "confirmation_id": confirmation_id })),
    )
}

#[tauri::command]
fn sam_proactive_run(state: State<'_, AppState>, task_id: String) -> CommandResult {
    invalid(validate::id(&task_id))?;
    run(
        &state,
        Route::ProactiveRun,
        Some(json!({ "task_id": task_id })),
    )
}

#[tauri::command]
fn sam_proactive_notification(
    state: State<'_, AppState>,
    notification_id: String,
    action: String,
) -> CommandResult {
    invalid(validate::id(&notification_id))?;
    invalid(validate::notification_action(&action))?;
    run(
        &state,
        Route::ProactiveNotification,
        Some(json!({ "notification_id": notification_id, "action": action })),
    )
}

/// The owner's switch for background scheduling: two booleans (on/off, and
/// whether to keep it on across restarts), nothing else. The backend refuses
/// it in Guest Mode and requires PROACTIVE/UPDATE.
#[tauri::command]
fn sam_proactive_scheduler(
    state: State<'_, AppState>,
    enabled: bool,
    remember: Option<bool>,
) -> CommandResult {
    // `remember` (Phase 17): the owner's explicit choice to keep scheduling on
    // across restarts. Only a boolean; the backend honours it only when enabling.
    run(
        &state,
        Route::ProactiveScheduler,
        Some(json!({ "enabled": enabled, "remember": remember.unwrap_or(false) })),
    )
}

/// Owner-approved career preferences: typed and closed. Unknown fields (a
/// permission, a path, a state) are refused by deserialization.
#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct CareerPreferencesArgs {
    preferred_roles: Vec<String>,
    locations: Vec<String>,
    work_modes: Vec<String>,
    role_types: Vec<String>,
    salary_preference: Option<String>,
    needs_sponsorship: Option<bool>,
    contact_name: Option<String>,
    contact_email: Option<String>,
    contact_phone: Option<String>,
    portfolio_url: Option<String>,
    linkedin_url: Option<String>,
}

#[tauri::command]
fn sam_career_overview(state: State<'_, AppState>) -> CommandResult {
    run(&state, Route::CareerOverview, None)
}

#[allow(clippy::too_many_arguments)]
#[tauri::command]
fn sam_career_opportunity(
    state: State<'_, AppState>,
    action: String,
    opportunity_id: Option<String>,
    draft_id: Option<String>,
    text: Option<String>,
    url: Option<String>,
    source_kind: Option<String>,
    opportunity_type: Option<String>,
    application_url: Option<String>,
    external_id: Option<String>,
) -> CommandResult {
    invalid(validate::career_opportunity_action(&action))?;
    invalid(validate::optional_id(opportunity_id.as_deref()))?;
    invalid(validate::optional_id(draft_id.as_deref()))?;
    invalid(validate::optional_text(
        text.as_deref(),
        validate::MAX_LISTING_CHARS,
    ))?;
    invalid(validate::optional_https_url(url.as_deref()))?;
    invalid(validate::optional_source_kind(source_kind.as_deref()))?;
    invalid(validate::optional_opportunity_type(
        opportunity_type.as_deref(),
    ))?;
    invalid(validate::optional_https_url(application_url.as_deref()))?;
    invalid(validate::optional_text(external_id.as_deref(), 200))?;
    run(
        &state,
        Route::CareerOpportunity,
        Some(json!({
            "action": action,
            "opportunity_id": opportunity_id,
            "draft_id": draft_id,
            "text": text,
            "url": url,
            "source_kind": source_kind,
            "opportunity_type": opportunity_type,
            "application_url": application_url,
            "external_id": external_id,
        })),
    )
}

#[tauri::command]
fn sam_career_fit(
    state: State<'_, AppState>,
    opportunity_id: String,
    contact_id: Option<String>,
) -> CommandResult {
    invalid(validate::id(&opportunity_id))?;
    invalid(validate::optional_id(contact_id.as_deref()))?;
    run(
        &state,
        Route::CareerFit,
        Some(json!({ "opportunity_id": opportunity_id, "contact_id": contact_id })),
    )
}

#[allow(clippy::too_many_arguments)]
#[tauri::command]
fn sam_career_draft(
    state: State<'_, AppState>,
    action: String,
    opportunity_id: Option<String>,
    draft_id: Option<String>,
    document_id: Option<String>,
    question_id: Option<String>,
    text: Option<String>,
    questions: Vec<String>,
    lines: Vec<String>,
    motivation: Option<String>,
    direction: Option<String>,
    confirmation_id: Option<String>,
) -> CommandResult {
    invalid(validate::career_draft_action(&action))?;
    for id in [
        &opportunity_id,
        &draft_id,
        &document_id,
        &question_id,
        &confirmation_id,
    ] {
        invalid(validate::optional_id(id.as_deref()))?;
    }
    let max = validate::MAX_CAREER_TEXT_CHARS;
    invalid(validate::optional_text(text.as_deref(), max))?;
    invalid(validate::optional_text(motivation.as_deref(), max))?;
    invalid(validate::optional_text(direction.as_deref(), max))?;
    invalid(validate::text_list(
        &questions,
        validate::MAX_CAREER_ITEMS,
        500,
    ))?;
    invalid(validate::text_list(&lines, validate::MAX_CAREER_LINES, max))?;
    run(
        &state,
        Route::CareerDraft,
        Some(json!({
            "action": action,
            "opportunity_id": opportunity_id,
            "draft_id": draft_id,
            "document_id": document_id,
            "question_id": question_id,
            "text": text,
            "questions": questions,
            "lines": lines,
            "motivation": motivation,
            "direction": direction,
            "confirmation_id": confirmation_id,
        })),
    )
}

#[tauri::command]
fn sam_career_submit(
    state: State<'_, AppState>,
    draft_id: String,
    confirmation_id: Option<String>,
) -> CommandResult {
    invalid(validate::id(&draft_id))?;
    invalid(validate::optional_id(confirmation_id.as_deref()))?;
    run(
        &state,
        Route::CareerSubmit,
        Some(json!({ "draft_id": draft_id, "confirmation_id": confirmation_id })),
    )
}

#[allow(clippy::too_many_arguments)]
#[tauri::command]
fn sam_career_contact(
    state: State<'_, AppState>,
    name: String,
    role: String,
    organization: String,
    source_kind: String,
    url: String,
    quote: String,
    email: Option<String>,
    opportunity_id: Option<String>,
    research_topics: Vec<String>,
) -> CommandResult {
    invalid(validate::text(&name, 200))?;
    invalid(validate::contact_role(&role))?;
    invalid(validate::text(&organization, 200))?;
    invalid(validate::source_kind(&source_kind))?;
    invalid(validate::https_url(&url))?;
    invalid(validate::text(&quote, 500))?;
    invalid(validate::optional_text(email.as_deref(), 254))?;
    invalid(validate::optional_id(opportunity_id.as_deref()))?;
    invalid(validate::text_list(&research_topics, 20, 80))?;
    run(
        &state,
        Route::CareerContact,
        Some(json!({
            "name": name,
            "role": role,
            "organization": organization,
            "source_kind": source_kind,
            "url": url,
            "quote": quote,
            "email": email,
            "opportunity_id": opportunity_id,
            "research_topics": research_topics,
        })),
    )
}

#[allow(clippy::too_many_arguments)]
#[tauri::command]
fn sam_career_outreach(
    state: State<'_, AppState>,
    action: String,
    outreach_id: Option<String>,
    contact_id: Option<String>,
    follow_up_id: Option<String>,
    kind: Option<String>,
    channel: Option<String>,
    opportunity_id: Option<String>,
    note: Option<String>,
) -> CommandResult {
    invalid(validate::career_outreach_action(&action))?;
    for id in [&outreach_id, &contact_id, &follow_up_id, &opportunity_id] {
        invalid(validate::optional_id(id.as_deref()))?;
    }
    invalid(validate::optional_outreach_kind(kind.as_deref()))?;
    invalid(validate::optional_channel(channel.as_deref()))?;
    invalid(validate::optional_text(
        note.as_deref(),
        validate::MAX_CAREER_TEXT_CHARS,
    ))?;
    run(
        &state,
        Route::CareerOutreach,
        Some(json!({
            "action": action,
            "outreach_id": outreach_id,
            "contact_id": contact_id,
            "follow_up_id": follow_up_id,
            "kind": kind,
            "channel": channel,
            "opportunity_id": opportunity_id,
            "note": note,
        })),
    )
}

#[tauri::command]
fn sam_career_send(
    state: State<'_, AppState>,
    outreach_id: String,
    confirmation_id: Option<String>,
    email_confirmation_id: Option<String>,
) -> CommandResult {
    invalid(validate::id(&outreach_id))?;
    invalid(validate::optional_id(confirmation_id.as_deref()))?;
    invalid(validate::optional_id(email_confirmation_id.as_deref()))?;
    run(
        &state,
        Route::CareerSend,
        Some(json!({
            "outreach_id": outreach_id,
            "confirmation_id": confirmation_id,
            "email_confirmation_id": email_confirmation_id,
        })),
    )
}

#[tauri::command]
fn sam_career_preferences(
    state: State<'_, AppState>,
    preferences: CareerPreferencesArgs,
) -> CommandResult {
    let p = preferences;
    invalid(validate::text_list(&p.preferred_roles, 20, 100))?;
    invalid(validate::text_list(&p.locations, 20, 100))?;
    invalid(validate::work_modes(&p.work_modes))?;
    invalid(validate::text_list(&p.role_types, 20, 100))?;
    invalid(validate::optional_text(p.salary_preference.as_deref(), 100))?;
    invalid(validate::optional_text(p.contact_name.as_deref(), 200))?;
    invalid(validate::optional_text(p.contact_email.as_deref(), 254))?;
    invalid(validate::optional_text(p.contact_phone.as_deref(), 40))?;
    invalid(validate::optional_https_url(p.portfolio_url.as_deref()))?;
    invalid(validate::optional_https_url(p.linkedin_url.as_deref()))?;
    run(
        &state,
        Route::CareerPreferences,
        Some(json!({
            "preferred_roles": p.preferred_roles,
            "locations": p.locations,
            "work_modes": p.work_modes,
            "role_types": p.role_types,
            "salary_preference": p.salary_preference,
            "needs_sponsorship": p.needs_sponsorship,
            "contact_name": p.contact_name,
            "contact_email": p.contact_email,
            "contact_phone": p.contact_phone,
            "portfolio_url": p.portfolio_url,
            "linkedin_url": p.linkedin_url,
        })),
    )
}

#[tauri::command]
fn sam_models_status(state: State<'_, AppState>) -> CommandResult {
    run(&state, Route::ModelsStatus, None)
}

#[allow(clippy::too_many_arguments)]
#[tauri::command]
fn sam_models_preferences(
    state: State<'_, AppState>,
    preferred_provider: Option<String>,
    allow_free_fallback: bool,
    personal_to_free_tier: bool,
    private_to_free_tier: bool,
    claude_improvement_state: String,
    private_to_claude_when_improvement_enabled: bool,
    gemini_attestation: String,
    topic_blocklist: Vec<String>,
    step_up: Option<String>,
) -> CommandResult {
    if let Some(provider) = &preferred_provider {
        invalid(validate::provider_id(provider))?;
    }
    invalid(validate::improvement_state(&claude_improvement_state))?;
    invalid(validate::gemini_attestation(&gemini_attestation))?;
    invalid(validate::blocklist(&topic_blocklist))?;
    if let Some(secret) = &step_up {
        invalid(validate::required_step_up(secret))?;
    }
    let mut body = json!({
        "preferred_provider": preferred_provider,
        "allow_free_fallback": allow_free_fallback,
        "personal_to_free_tier": personal_to_free_tier,
        "private_to_free_tier": private_to_free_tier,
        "claude_improvement_state": claude_improvement_state,
        "private_to_claude_when_improvement_enabled": private_to_claude_when_improvement_enabled,
        "gemini_attestation": gemini_attestation,
        "topic_blocklist": topic_blocklist,
    });
    if let Some(secret) = step_up {
        body["step_up"] = json!(secret);
    }
    run(&state, Route::ModelsPreferences, Some(body))
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
/// Release builds own their backend (the bundled, trusted sidecar); a debug
/// build talks to the developer's backend configured in its environment. A
/// release build NEVER falls back to an environment-configured backend: if its
/// own backend cannot start (bounded by `sidecar::READY_TIMEOUT`), every
/// bridge call is `unavailable`.
///
/// The window opens immediately; while the owned backend starts on a
/// background thread, bridge calls answer `starting` so the webview can show
/// Sam starting up instead of an error.
fn start_backend(slot: Arc<BackendSlot>, owned: Arc<Mutex<Owned>>) {
    if cfg!(debug_assertions) {
        slot.set(Slot::Ready(Backend::from_env()));
        return;
    }
    std::thread::spawn(move || match sidecar::start_owned() {
        Ok(mut sidecar) => {
            let Ok(mut guard) = owned.lock() else {
                sidecar.stop();
                return;
            };
            if guard.exiting {
                // Sam quit while its backend was starting: never leave it behind.
                sidecar.stop();
                return;
            }
            slot.set(Slot::Ready(Backend::new(
                sidecar.port,
                Some(sidecar.token.clone()),
            )));
            guard.sidecar = Some(sidecar);
        }
        Err(_) => slot.set(Slot::Failed),
    });
}

#[derive(Default)]
struct Owned {
    sidecar: Option<sidecar::Sidecar>,
    exiting: bool,
}

/// Headless verification of a built artifact (see `sidecar::self_test`).
pub fn self_test(data_dir: &str) -> i32 {
    sidecar::self_test(data_dir)
}

pub fn run_app() {
    let slot = Arc::new(BackendSlot::new(Slot::Starting));
    let owned = Arc::new(Mutex::new(Owned::default()));
    start_backend(slot.clone(), owned.clone());
    tauri::Builder::default()
        .manage(AppState { backend: slot })
        .invoke_handler(tauri::generate_handler![
            sam_status,
            sam_chat,
            sam_knowledge_list,
            sam_knowledge_query,
            sam_knowledge_ingest,
            sam_knowledge_remove,
            sam_memory_search,
            sam_tools,
            sam_permissions,
            sam_revoke_grant,
            sam_activity,
            sam_decide_confirmation,
            sam_voice_utterance,
            sam_speak,
            sam_identity_status,
            sam_identity_enroll_begin,
            sam_identity_enroll_sample,
            sam_identity_enroll_complete,
            sam_identity_enroll_cancel,
            sam_identity_delete,
            sam_guest_challenge,
            sam_guest_start,
            sam_guest_end,
            sam_models_status,
            sam_models_preferences,
            sam_professional_profile,
            sam_professional_ingest,
            sam_professional_review,
            sam_professional_remove,
            sam_professional_query,
            sam_proactive_overview,
            sam_proactive_create,
            sam_proactive_update,
            sam_proactive_delete,
            sam_proactive_run,
            sam_proactive_notification,
            sam_proactive_scheduler,
            sam_career_overview,
            sam_career_opportunity,
            sam_career_fit,
            sam_career_draft,
            sam_career_submit,
            sam_career_contact,
            sam_career_outreach,
            sam_career_send,
            sam_career_preferences,
            sam_voice_wake,
            sam_voice_activation,
        ])
        .build(tauri::generate_context!())
        .expect("error while building Sam desktop")
        .run(move |_app, event| {
            if let tauri::RunEvent::Exit = event {
                if let Ok(mut guard) = owned.lock() {
                    guard.exiting = true;
                    if let Some(mut backend) = guard.sidecar.take() {
                        backend.stop();
                    }
                }
            }
        });
}
