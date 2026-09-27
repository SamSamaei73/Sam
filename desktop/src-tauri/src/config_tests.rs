//! Static assertions about the shipped Tauri configuration: minimal
//! capabilities, restrictive CSP, and no generic command surface.

use serde_json::Value;

const CONF: &str = include_str!("../tauri.conf.json");
const CAPABILITY: &str = include_str!("../capabilities/default.json");
const BUILD_RS: &str = include_str!("../build.rs");
const LIB_RS: &str = include_str!("lib.rs");
const BACKEND_RS: &str = include_str!("backend.rs");
const CARGO: &str = include_str!("../Cargo.toml");

const COMMANDS: [&str; 46] = [
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
    assert_eq!(BUILD_RS.matches("\"sam_").count(), 46);
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
    assert_eq!(LIB_RS.matches("#[tauri::command]").count(), 46);
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
