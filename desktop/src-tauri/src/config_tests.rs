//! Static assertions about the shipped Tauri configuration: minimal
//! capabilities, restrictive CSP, and no generic command surface.

use serde_json::Value;

const CONF: &str = include_str!("../tauri.conf.json");
const CAPABILITY: &str = include_str!("../capabilities/default.json");
const BUILD_RS: &str = include_str!("../build.rs");
const LIB_RS: &str = include_str!("lib.rs");
const BACKEND_RS: &str = include_str!("backend.rs");
const SIDECAR_RS: &str = include_str!("sidecar.rs");
const MAIN_RS: &str = include_str!("main.rs");
const CARGO: &str = include_str!("../Cargo.toml");
const BACKEND_PATHS: &str = include_str!("../../../src/sam/storage/paths.py");

const COMMANDS: [&str; 48] = [
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
    "sam_identity_status",
    "sam_identity_enroll_begin",
    "sam_identity_enroll_sample",
    "sam_identity_enroll_complete",
    "sam_identity_enroll_cancel",
    "sam_identity_delete",
    "sam_guest_challenge",
    "sam_guest_start",
    "sam_guest_end",
    "sam_models_status",
    "sam_models_preferences",
    "sam_professional_profile",
    "sam_professional_ingest",
    "sam_professional_review",
    "sam_professional_remove",
    "sam_professional_query",
    "sam_proactive_overview",
    "sam_proactive_create",
    "sam_proactive_update",
    "sam_proactive_delete",
    "sam_proactive_run",
    "sam_proactive_notification",
    "sam_proactive_scheduler",
    "sam_career_overview",
    "sam_career_opportunity",
    "sam_career_fit",
    "sam_career_draft",
    "sam_career_submit",
    "sam_career_contact",
    "sam_career_outreach",
    "sam_career_send",
    "sam_career_preferences",
    "sam_voice_wake",
    "sam_voice_activation",
];

fn conf() -> Value {
    serde_json::from_str(CONF).unwrap()
}

#[test]
fn capability_grants_only_the_fixed_sam_commands() {
    let cap: Value = serde_json::from_str(CAPABILITY).unwrap();
    let mut got: Vec<String> = cap["permissions"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_str().unwrap().to_string())
        .collect();
    got.sort();
    let mut want: Vec<String> = COMMANDS
        .iter()
        .map(|c| format!("allow-{}", c.replace('_', "-")))
        .collect();
    want.sort();
    assert_eq!(got, want);
    assert_eq!(cap["windows"], serde_json::json!(["main"]));
    assert!(cap.get("remote").is_none(), "no remote webview access");
}

#[test]
fn capability_has_no_broad_plugin_or_core_permissions() {
    let cap: Value = serde_json::from_str(CAPABILITY).unwrap();
    let permissions = cap["permissions"].to_string();
    for banned in [
        "shell",
        "fs:",
        "process",
        "clipboard",
        "global-shortcut",
        "http",
        "opener",
        "dialog",
        "core:default",
        "core:window",
        "core:webview",
        "webview:allow-create",
        "window:allow-create",
    ] {
        assert!(
            !permissions.contains(banned),
            "capability must not contain {banned}"
        );
    }
}

/// Phase 17: the bundle identity is the backend's data-directory identity
/// (``~/Library/Application Support/app.sam.desktop``), and a release build
/// uses the bundled frontend, never a development server.
#[test]
fn app_identity_matches_the_backend_data_directory_and_release_needs_no_dev_server() {
    let c = conf();
    assert_eq!(c["identifier"], "app.sam.desktop");
    assert!(BACKEND_PATHS.contains("APP_IDENTIFIER = \"app.sam.desktop\""));
    assert_eq!(c["build"]["frontendDist"], "../dist");
    assert!(c["build"]["devUrl"]
        .as_str()
        .unwrap()
        .starts_with("http://127.0.0.1:"));
    assert!(c["app"].get("devtools").is_none());
    assert!(!CARGO.contains("devtools"));
}

#[test]
fn csp_is_restrictive_and_has_no_remote_sources() {
    let csp = conf()["app"]["security"]["csp"]
        .as_str()
        .unwrap()
        .to_string();
    assert!(csp.starts_with("default-src 'none'"));
    assert!(csp.contains("script-src 'self'"));
    assert!(csp.contains("object-src 'none'"));
    assert!(csp.contains("frame-ancestors 'none'"));
    assert!(!csp.contains("'unsafe-eval'"));
    assert!(!csp.contains("script-src 'self' 'unsafe-inline'"));
    assert!(!csp.contains("https:"));
    assert!(!csp.contains(" *"));
    // No direct network from the webview: only the Tauri IPC scheme.
    let connect: Vec<&str> = csp
        .split(';')
        .map(str::trim)
        .filter(|d| d.starts_with("connect-src"))
        .collect();
    assert_eq!(connect, vec!["connect-src ipc: http://ipc.localhost"]);
}

#[test]
fn window_is_single_and_fixed_no_remote_urls() {
    let c = conf();
    let windows = c["app"]["windows"].as_array().unwrap();
    assert_eq!(windows.len(), 1);
    assert!(
        windows[0].get("url").is_none(),
        "no remote/alternate window url"
    );
    assert_eq!(c["app"]["withGlobalTauri"], false);
    assert_eq!(c["app"]["security"]["freezePrototype"], true);
    assert!(c["app"].get("macOSPrivateApi").is_none());
}

#[test]
fn build_script_declares_the_same_command_list_as_the_handler() {
    for command in COMMANDS {
        assert!(
            BUILD_RS.contains(&format!("\"{command}\"")),
            "build.rs missing {command}"
        );
        assert!(
            LIB_RS.contains(&format!("            {command},")),
            "handler missing {command}"
        );
    }
    assert_eq!(BUILD_RS.matches("\"sam_").count(), 48);
}

#[test]
fn no_generic_or_privileged_commands_exist() {
    for banned in [
        "execute_shell",
        "execute_command",
        "run_shell",
        "read_arbitrary_file",
        "write_arbitrary_file",
        "fetch_arbitrary_url",
        "proxy_arbitrary_request",
        "invoke_arbitrary_endpoint",
        "Command::new",
        "std::process",
        "std::fs",
        "tauri_plugin_shell",
        "tauri_plugin_fs",
        "tauri_plugin_http",
    ] {
        assert!(!LIB_RS.contains(banned), "lib.rs must not contain {banned}");
        assert!(
            !BACKEND_RS.contains(banned),
            "backend.rs must not contain {banned}"
        );
    }
    assert_eq!(LIB_RS.matches("#[tauri::command]").count(), 48);
}

/// The ONLY exception to the "no URL arguments" rule, reviewed for Phase 16:
/// Career links that are provenance DATA (where a listing or contact was seen,
/// the owner's own portfolio/LinkedIn links, and the official application page
/// recorded from an official listing). Neither the shell nor the backend ever
/// requests any of them; the backend validates them as https and a submission
/// target must match the official host exactly. Every other command still takes
/// no URL, endpoint, scope, risk, principal, token or model.
const CAREER_LINK_DATA: [&str; 5] = [
    "url: Option<String>,",
    "application_url: Option<String>,",
    "url: String,",
    "portfolio_url: Option<String>,",
    "linkedin_url: Option<String>,",
];

#[test]
fn career_link_data_is_confined_to_career_commands() {
    for (index, line) in LIB_RS.lines().enumerate() {
        if CAREER_LINK_DATA.contains(&line.trim()) {
            let preceding: Vec<&str> = LIB_RS.lines().take(index).collect();
            let owner = preceding
                .iter()
                .rev()
                .find(|l| l.starts_with("fn sam_") || l.starts_with("struct "))
                .copied()
                .unwrap_or_default();
            assert!(
                owner.starts_with("fn sam_career_") || owner == "struct CareerPreferencesArgs {",
                "{line} outside a Career command: {owner}"
            );
        }
    }
}

/// Career link DATA must never become a URL-opening or network capability:
/// no opener/shell/browser crate or plugin, no call that opens or fetches a
/// URL, and the only HTTP client talks to the fixed loopback backend.
#[test]
fn career_link_data_cannot_open_or_fetch_anything() {
    for banned in [
        "tauri-plugin-opener",
        "tauri-plugin-shell",
        "tauri-plugin-http",
        "webbrowser",
        "open =",
        "opener",
        "reqwest",
        "hyper",
    ] {
        assert!(
            !CARGO.contains(banned),
            "Cargo.toml must not contain {banned}"
        );
    }
    for source in [LIB_RS, BACKEND_RS] {
        for banned in [
            "open::",
            "webbrowser::",
            "opener",
            ".shell()",
            "open_url",
            "Url::parse",
            "WebviewUrl::External",
            "WebviewWindowBuilder",
            "navigate(",
        ] {
            assert!(!source.contains(banned), "shell must not contain {banned}");
        }
    }
    // The backend host is a constant; a career URL can only ever travel as a
    // JSON body field to that loopback backend, which treats it as data.
    assert!(BACKEND_RS.contains("pub const BACKEND_HOST: &str = \"127.0.0.1\";"));
    let career_fns: Vec<&str> = LIB_RS
        .split("#[tauri::command]")
        .filter(|f| f.contains("fn sam_career_"))
        .collect();
    assert_eq!(career_fns.len(), 9);
    for body in career_fns {
        let forwarded = body.split("\n}").next().unwrap_or_default();
        assert!(
            forwarded.contains("run(") && forwarded.contains("Route::Career"),
            "career command must only forward to the fixed backend route"
        );
    }
}

#[test]
fn commands_take_no_authority_or_destination_arguments() {
    let signatures: String = LIB_RS
        .lines()
        .filter(|l| {
            l.trim_start().starts_with("fn sam_") || l.starts_with("    ") && l.contains(": ")
        })
        .collect::<Vec<_>>()
        .join("\n");
    for banned in [
        "principal",
        "scope",
        "risk",
        "url",
        "endpoint",
        "token",
        "api_key",
        "model",
        "voice_reference",
        "header",
        "method",
    ] {
        for line in signatures
            .lines()
            .filter(|l| l.contains(": String") || l.contains(": Option"))
            .filter(|l| !CAREER_LINK_DATA.contains(&l.trim()))
        {
            assert!(
                !line.contains(banned),
                "argument `{line}` looks like authority/destination"
            );
        }
    }
}

#[test]
fn http_client_is_plaintext_loopback_only() {
    assert!(CARGO.contains("default-features = false"));
    assert!(!CARGO.contains("rustls"));
    assert!(!CARGO.contains("native-tls"));
    assert!(BACKEND_RS.contains("max_redirects(0)"));
    assert!(BACKEND_RS.contains(".proxy(None)"));
    assert!(BACKEND_RS.contains("pub const BACKEND_HOST: &str = \"127.0.0.1\""));
}

#[test]
fn tauri_dependency_does_not_enable_devtools_or_extra_features() {
    assert!(CARGO.contains("tauri = { version = \"2\", features = [] }"));
    assert!(!CARGO.contains("devtools"));
    assert!(!CARGO.contains("protocol-asset"));
}

/// Phase 17 remediation: the ONLY process the app ever starts is its own
/// bundled backend, by argv, with a cleared environment and no credential.
#[test]
fn the_backend_is_spawned_only_by_the_sidecar_with_fixed_argv_and_clean_env() {
    // Production code only: no comments, no unit tests.
    let code: String = SIDECAR_RS
        .split("#[cfg(test)]")
        .next()
        .unwrap()
        .lines()
        .filter(|l| !l.trim_start().starts_with("//"))
        .collect::<Vec<_>>()
        .join("\n");
    assert_eq!(SIDECAR_RS.matches("Command::new(").count(), 1);
    for source in [LIB_RS, BACKEND_RS, MAIN_RS] {
        assert!(
            !source.contains("Command::new"),
            "only sidecar.rs may spawn"
        );
    }
    assert!(SIDECAR_RS.contains("Command::new(python)"));
    assert!(SIDECAR_RS.contains(r#".args(["-I", "-B", "-m", BACKEND_MODULE])"#));
    assert!(SIDECAR_RS.contains(r#"pub const BACKEND_MODULE: &str = "sam.system.backend";"#));
    assert!(SIDECAR_RS.contains(".env_clear()"));
    assert!(SIDECAR_RS.contains(r#".env("APP_ENV", "production")"#));
    assert!(SIDECAR_RS.contains(".stdout(Stdio::null())"));
    assert!(SIDECAR_RS.contains(".stderr(Stdio::null())"));
    assert!(SIDECAR_RS.contains("Resources/backend/python/bin/python3.12"));
    assert!(SIDECAR_RS.contains("if !python.starts_with(&contents)"));
    for banned in [
        "\"/bin/sh\"",
        "\"sh\"",
        "bash",
        "\"-c\"",
        "API_KEY",
        "GEMINI",
        "ANTHROPIC",
        "FISH",
        "PYTHONPATH",
        "reload",
        "0.0.0.0",
        ".venv",
    ] {
        assert!(!code.contains(banned), "sidecar must not contain {banned}");
    }
}

/// A release build owns its backend and never falls back to an
/// environment-configured (developer) backend.
#[test]
fn release_builds_never_use_an_environment_configured_backend() {
    let start = LIB_RS.find("fn start_backend(").expect("start_backend");
    let body = &LIB_RS[start..start + LIB_RS[start..].find("\n}\n").unwrap()];
    assert_eq!(body.matches("Backend::from_env()").count(), 1);
    let debug = body.find("if cfg!(debug_assertions)").expect("debug gate");
    let from_env = body.find("Backend::from_env()").unwrap();
    let release = body.find("sidecar::start_owned()").expect("owned backend");
    assert!(debug < from_env && from_env < release);
    assert!(body.contains("Err(_) => slot.set(Slot::Failed)"));
    assert_eq!(LIB_RS.matches("Backend::from_env()").count(), 1);
}

/// The window never waits on the backend, and quitting always stops it, even
/// when Sam quits while the backend is still starting.
#[test]
fn owned_backend_starts_in_the_background_and_always_stops() {
    let start = LIB_RS.find("fn start_backend(").expect("start_backend");
    let body = &LIB_RS[start..start + LIB_RS[start..].find("\n}\n").unwrap()];
    assert!(body.contains("std::thread::spawn(move || match sidecar::start_owned()"));
    assert!(body.contains("if guard.exiting {"));
    assert!(LIB_RS.contains(".manage(AppState { backend: slot })"));
    assert!(LIB_RS.contains("BackendSlot::new(Slot::Starting)"));
    let exit = LIB_RS.find("tauri::RunEvent::Exit").expect("exit handler");
    let handler = &LIB_RS[exit..];
    assert!(handler.contains("guard.exiting = true;"));
    assert!(handler.contains("backend.stop();"));
}
