/// Every command the webview may call is declared here. Only these become
/// ACL permissions (`allow-<command>`); nothing else is invokable.
const COMMANDS: &[&str] = &[
    "sam_status",
    "sam_chat",
    "sam_knowledge_list",
    "sam_knowledge_query",
    "sam_knowledge_ingest",
    "sam_knowledge_remove",
    "sam_memory_search",
    "sam_tools",
    "sam_permissions",
    "sam_revoke_grant",
    "sam_activity",
    "sam_decide_confirmation",
    "sam_voice_utterance",
    "sam_speak",
];

fn main() {
    tauri_build::try_build(
        tauri_build::Attributes::new()
            .app_manifest(tauri_build::AppManifest::new().commands(COMMANDS)),
    )
    .expect("failed to run tauri-build");
}
