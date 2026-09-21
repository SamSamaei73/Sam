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
mod validate;

use backend::{Backend, BridgeError, Route};
use serde_json::{json, Value};
use tauri::State;

pub struct AppState {
    backend: Backend,
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
    run(&state, Route::Status, None)
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
) -> CommandResult {
    invalid(validate::base64_payload(
        &audio_base64,
        validate::MAX_AUDIO_BASE64,
    ))?;
    invalid(validate::optional_id(confirmation_id.as_deref()))?;
    run(
        &state,
        Route::VoiceUtterance,
        Some(json!({ "audio_base64": audio_base64, "confirmation_id": confirmation_id })),
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
pub fn run_app() {
    tauri::Builder::default()
        .manage(AppState {
            backend: Backend::from_env(),
        })
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
        ])
        .run(tauri::generate_context!())
        .expect("error while running Sam desktop");
}
