//! The one and only network path out of the desktop app.
//!
//! Everything here is deliberately narrow:
//!
//! * the destination host is the constant `127.0.0.1` — never configurable,
//!   never derived from a request, never from the webview;
//! * the path is one of the fixed [`Route`] constants — the webview cannot
//!   supply a path, query string, header, method or URL;
//! * bodies and responses are size-bounded, calls have a finite timeout,
//!   redirects are never followed and environment proxies are ignored;
//! * failures are reduced to a short code (see [`BridgeError`]); no backend
//!   body, header, URL, token or OS error text ever reaches the webview.
//!
//! Nothing here decides authorization. The backend's PermissionEngine does.

use serde_json::Value;
use std::time::Duration;

/// Loopback only. Not configurable.
pub const BACKEND_HOST: &str = "127.0.0.1";
pub const DEFAULT_PORT: u16 = 8000;
pub const TOKEN_HEADER: &str = "X-Sam-Desktop-Token";
pub const TOKEN_ENV: &str = "DESKTOP_BRIDGE_TOKEN";
pub const PORT_ENV: &str = "SAM_BACKEND_PORT";
pub const MIN_TOKEN_CHARS: usize = 32;

pub const MAX_REQUEST_BYTES: usize = 30_000_000;
pub const MAX_RESPONSE_BYTES: u64 = 40_000_000;
pub const SHORT_TIMEOUT: Duration = Duration::from_secs(20);
pub const LONG_TIMEOUT: Duration = Duration::from_secs(120);

/// The complete list of backend endpoints this app can ever call.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Route {
    Status,
    Chat,
    KnowledgeList,
    KnowledgeQuery,
    KnowledgeIngest,
    KnowledgeRemove,
    MemorySearch,
    Tools,
    Permissions,
    RevokeGrant,
    Activity,
    DecideConfirmation,
    VoiceUtterance,
    Speak,
}

impl Route {
    #[cfg(test)]
    pub const ALL: [Route; 14] = [
        Route::Status,
        Route::Chat,
        Route::KnowledgeList,
        Route::KnowledgeQuery,
        Route::KnowledgeIngest,
        Route::KnowledgeRemove,
        Route::MemorySearch,
        Route::Tools,
        Route::Permissions,
        Route::RevokeGrant,
        Route::Activity,
        Route::DecideConfirmation,
        Route::VoiceUtterance,
        Route::Speak,
    ];

    pub fn path(self) -> &'static str {
        match self {
            Route::Status => "/desktop/v1/status",
            Route::Chat => "/desktop/v1/chat",
            Route::KnowledgeList => "/desktop/v1/knowledge/resources",
            Route::KnowledgeQuery => "/desktop/v1/knowledge/query",
            Route::KnowledgeIngest => "/desktop/v1/knowledge/ingest",
            Route::KnowledgeRemove => "/desktop/v1/knowledge/remove",
            Route::MemorySearch => "/desktop/v1/memory/search",
            Route::Tools => "/desktop/v1/tools",
            Route::Permissions => "/desktop/v1/permissions",
            Route::RevokeGrant => "/desktop/v1/permissions/revoke",
            Route::Activity => "/desktop/v1/activity",
            Route::DecideConfirmation => "/desktop/v1/confirmations/decide",
            Route::VoiceUtterance => "/desktop/v1/voice/utterance",
            Route::Speak => "/desktop/v1/tts/speak",
        }
    }

    pub fn is_get(self) -> bool {
        matches!(
            self,
            Route::Status
                | Route::KnowledgeList
                | Route::Tools
                | Route::Permissions
                | Route::Activity
        )
    }

    pub fn timeout(self) -> Duration {
        match self {
            Route::Chat | Route::VoiceUtterance | Route::Speak | Route::KnowledgeIngest => {
                LONG_TIMEOUT
            }
            _ => SHORT_TIMEOUT,
        }
    }
}

/// The only failures the webview can observe. `as_code` is the whole message.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BridgeError {
    Unavailable,
    Timeout,
    Unauthorized,
    StepUpUnavailable,
    StepUpFailed,
    StepUpLocked,
    NotConfigured,
    TooLarge,
    Invalid,
    Failed,
}

impl BridgeError {
    pub fn as_code(self) -> &'static str {
        match self {
            BridgeError::Unavailable => "unavailable",
            BridgeError::Timeout => "timeout",
            BridgeError::Unauthorized => "unauthorized",
            BridgeError::StepUpUnavailable => "step_up_unavailable",
            BridgeError::StepUpFailed => "step_up_failed",
            BridgeError::StepUpLocked => "step_up_locked",
            BridgeError::NotConfigured => "not_configured",
            BridgeError::TooLarge => "too_large",
            BridgeError::Invalid => "invalid",
            BridgeError::Failed => "failed",
        }
    }
}

/// Parse the backend port. Only an unprivileged numeric port is accepted;
/// anything else falls back to the default rather than erroring or echoing.
pub fn parse_port(raw: Option<&str>) -> u16 {
    raw.and_then(|value| value.trim().parse::<u16>().ok())
        .filter(|port| *port >= 1024)
        .unwrap_or(DEFAULT_PORT)
}

pub fn valid_token(raw: Option<&str>) -> Option<String> {
    let token = raw?.trim();
    (token.chars().count() >= MIN_TOKEN_CHARS).then(|| token.to_string())
}

pub struct Backend {
    port: u16,
    token: Option<String>,
}

impl Backend {
    pub fn new(port: u16, token: Option<String>) -> Self {
        Backend { port, token }
    }

    /// Reads the token and port from the process environment, in Rust only.
    /// Neither value is ever passed to the webview.
    pub fn from_env() -> Self {
        Backend::new(
            parse_port(std::env::var(PORT_ENV).ok().as_deref()),
            valid_token(std::env::var(TOKEN_ENV).ok().as_deref()),
        )
    }

    pub fn url(&self, route: Route) -> String {
        format!("http://{}:{}{}", BACKEND_HOST, self.port, route.path())
    }

    pub fn call(&self, route: Route, body: Option<Value>) -> Result<Value, BridgeError> {
        let token = self.token.as_deref().ok_or(BridgeError::NotConfigured)?;
        let payload = match (&body, route.is_get()) {
            (_, true) => None,
            (Some(value), false) => {
                let bytes = serde_json::to_vec(value).map_err(|_| BridgeError::Invalid)?;
                if bytes.len() > MAX_REQUEST_BYTES {
                    return Err(BridgeError::TooLarge);
                }
                Some(bytes)
            }
            (None, false) => return Err(BridgeError::Invalid),
        };

        let agent: ureq::Agent = ureq::Agent::config_builder()
            .max_redirects(0)
            .http_status_as_error(false)
            .proxy(None)
            .timeout_global(Some(route.timeout()))
            .build()
            .into();

        let url = self.url(route);
        let result = match &payload {
            None => agent.get(&url).header(TOKEN_HEADER, token).call(),
            Some(bytes) => agent
                .post(&url)
                .header(TOKEN_HEADER, token)
                .content_type("application/json")
                .send(&bytes[..]),
        };
        let mut response = result.map_err(classify_transport)?;

        let status = response.status().as_u16();
        let bytes = response
            .body_mut()
            .with_config()
            .limit(MAX_RESPONSE_BYTES)
            .read_to_vec()
            .map_err(classify_transport)?;

        match status {
            200..=299 => serde_json::from_slice(&bytes).map_err(|_| BridgeError::Failed),
            403 => Err(step_up_code(&bytes).unwrap_or(BridgeError::Unauthorized)),
            401 => Err(BridgeError::Unauthorized),
            413 => Err(BridgeError::TooLarge),
            400 | 422 => Err(BridgeError::Invalid),
            503 => Err(BridgeError::NotConfigured),
            _ => Err(BridgeError::Failed),
        }
    }
}

/// The backend reports step-up outcomes as `{"detail":{"code":"step_up_*"}}`.
/// Only these three allowlisted codes are surfaced; nothing else from the body.
fn step_up_code(body: &[u8]) -> Option<BridgeError> {
    let value: Value = serde_json::from_slice(body).ok()?;
    match value.get("detail")?.get("code")?.as_str()? {
        "step_up_unavailable" => Some(BridgeError::StepUpUnavailable),
        "step_up_failed" => Some(BridgeError::StepUpFailed),
        "step_up_locked" => Some(BridgeError::StepUpLocked),
        _ => None,
    }
}

fn classify_transport(error: ureq::Error) -> BridgeError {
    match error {
        ureq::Error::Timeout(_) => BridgeError::Timeout,
        ureq::Error::BodyExceedsLimit(_) => BridgeError::TooLarge,
        ureq::Error::Io(_) | ureq::Error::ConnectionFailed | ureq::Error::HostNotFound => {
            BridgeError::Unavailable
        }
        _ => BridgeError::Failed,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::sync::mpsc;
    use std::thread;

    const TOKEN: &str = "0123456789abcdef0123456789abcdef-token";

    /// One-shot loopback server returning a canned raw HTTP response. Returns
    /// the port and a receiver yielding the raw request it got.
    fn serve(raw_response: Vec<u8>) -> (u16, mpsc::Receiver<Vec<u8>>) {
        let listener = TcpListener::bind((BACKEND_HOST, 0)).unwrap();
        let port = listener.local_addr().unwrap().port();
        let (tx, rx) = mpsc::channel();
        thread::spawn(move || {
            if let Ok((mut stream, _)) = listener.accept() {
                let mut buf = vec![0u8; 65536];
                let mut got = Vec::new();
                stream
                    .set_read_timeout(Some(Duration::from_millis(400)))
                    .ok();
                while let Ok(n) = stream.read(&mut buf) {
                    if n == 0 {
                        break;
                    }
                    got.extend_from_slice(&buf[..n]);
                    if got.windows(4).any(|w| w == b"\r\n\r\n") && n < buf.len() {
                        break;
                    }
                }
                let _ = tx.send(got);
                let _ = stream.write_all(&raw_response);
            }
        });
        (port, rx)
    }

    fn http(status: &str, extra: &str, body: &str) -> Vec<u8> {
        format!(
            "HTTP/1.1 {status}\r\n{extra}Content-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        )
        .into_bytes()
    }

    fn backend(port: u16) -> Backend {
        Backend::new(port, Some(TOKEN.to_string()))
    }

    #[test]
    fn host_is_the_loopback_constant() {
        assert_eq!(BACKEND_HOST, "127.0.0.1");
        for route in Route::ALL {
            let url = Backend::new(8000, None).url(route);
            assert!(
                url.starts_with("http://127.0.0.1:8000/desktop/v1/"),
                "{url}"
            );
        }
    }

    #[test]
    fn routes_are_fixed_and_unique() {
        let mut paths: Vec<&str> = Route::ALL.iter().map(|r| r.path()).collect();
        paths.sort_unstable();
        paths.dedup();
        assert_eq!(paths.len(), 14);
        for path in paths {
            assert!(path.starts_with("/desktop/v1/"));
            assert!(!path.contains('?') && !path.contains(".."));
        }
    }

    #[test]
    fn port_parsing_rejects_privileged_and_garbage() {
        assert_eq!(parse_port(None), 8000);
        assert_eq!(parse_port(Some("9000")), 9000);
        assert_eq!(parse_port(Some("80")), 8000);
        assert_eq!(parse_port(Some("evil.example:80")), 8000);
        assert_eq!(parse_port(Some("99999")), 8000);
    }

    #[test]
    fn short_or_missing_tokens_are_rejected() {
        assert!(valid_token(None).is_none());
        assert!(valid_token(Some("short")).is_none());
        assert!(valid_token(Some(TOKEN)).is_some());
    }

    #[test]
    fn missing_token_fails_before_any_network_use() {
        let backend = Backend::new(1, None);
        assert_eq!(
            backend.call(Route::Status, None),
            Err(BridgeError::NotConfigured)
        );
    }

    #[test]
    fn sends_the_token_header_and_the_fixed_path() {
        let (port, rx) = serve(http("200 OK", "", "{\"ok\":true}"));
        let value = backend(port).call(Route::Status, None).unwrap();
        assert_eq!(value["ok"], true);
        let request = String::from_utf8_lossy(&rx.recv().unwrap()).to_string();
        assert!(
            request.starts_with("GET /desktop/v1/status HTTP/1.1"),
            "{request}"
        );
        assert!(request
            .to_lowercase()
            .contains(&format!("x-sam-desktop-token: {}", TOKEN.to_lowercase())));
    }

    #[test]
    fn post_sends_json_to_the_fixed_path() {
        let (port, rx) = serve(http("200 OK", "", "{}"));
        backend(port)
            .call(Route::Chat, Some(serde_json::json!({"message": "hi"})))
            .unwrap();
        let request = String::from_utf8_lossy(&rx.recv().unwrap()).to_string();
        assert!(request.starts_with("POST /desktop/v1/chat HTTP/1.1"));
        assert!(request.contains("{\"message\":\"hi\"}"));
    }

    #[test]
    fn redirects_are_never_followed() {
        let (port, _rx) = serve(http(
            "302 Found",
            "Location: http://example.invalid/\r\n",
            "",
        ));
        let result = backend(port).call(Route::Status, None);
        assert!(result.is_err());
        assert_ne!(result, Ok(serde_json::Value::Null));
    }

    #[test]
    fn oversized_request_is_rejected_locally() {
        let big = "a".repeat(MAX_REQUEST_BYTES + 1);
        let result = Backend::new(1, Some(TOKEN.to_string()))
            .call(Route::Chat, Some(serde_json::json!({ "message": big })));
        assert_eq!(result, Err(BridgeError::TooLarge));
    }

    #[test]
    fn post_without_a_body_is_invalid() {
        assert_eq!(
            Backend::new(1, Some(TOKEN.to_string())).call(Route::Chat, None),
            Err(BridgeError::Invalid)
        );
    }

    #[test]
    fn http_errors_become_sanitized_codes_without_leaking_bodies() {
        let cases = [
            ("401 Unauthorized", BridgeError::Unauthorized),
            ("403 Forbidden", BridgeError::Unauthorized),
            ("413 Payload Too Large", BridgeError::TooLarge),
            ("422 Unprocessable Entity", BridgeError::Invalid),
            ("503 Service Unavailable", BridgeError::NotConfigured),
            ("500 Internal Server Error", BridgeError::Failed),
        ];
        for (status, expected) in cases {
            let (port, _rx) = serve(http(
                status,
                "",
                "{\"detail\":\"secret-trace /etc/passwd\"}",
            ));
            let error = backend(port).call(Route::Status, None).unwrap_err();
            assert_eq!(error, expected, "{status}");
            assert!(!error.as_code().contains("secret"));
            assert!(!error.as_code().contains("passwd"));
        }
    }

    #[test]
    fn step_up_codes_are_surfaced_but_other_bodies_are_not() {
        let cases = [
            ("step_up_unavailable", BridgeError::StepUpUnavailable),
            ("step_up_failed", BridgeError::StepUpFailed),
            ("step_up_locked", BridgeError::StepUpLocked),
            ("bridge_forbidden", BridgeError::Unauthorized),
            ("anything-else secret", BridgeError::Unauthorized),
        ];
        for (code, expected) in cases {
            let body = format!("{{\"detail\":{{\"code\":\"{code}\"}}}}");
            let (port, _rx) = serve(http("403 Forbidden", "", &body));
            let error = backend(port)
                .call(Route::DecideConfirmation, Some(serde_json::json!({})))
                .unwrap_err();
            assert_eq!(error, expected, "{code}");
        }
    }

    #[test]
    fn unreachable_backend_is_unavailable() {
        // Bind then drop to obtain a port that is (almost certainly) closed.
        let port = TcpListener::bind((BACKEND_HOST, 0))
            .unwrap()
            .local_addr()
            .unwrap()
            .port();
        assert_eq!(
            backend(port).call(Route::Status, None),
            Err(BridgeError::Unavailable)
        );
    }

    #[test]
    fn non_json_success_body_is_a_generic_failure() {
        let (port, _rx) = serve(http("200 OK", "", "not json <html>"));
        assert_eq!(
            backend(port).call(Route::Status, None),
            Err(BridgeError::Failed)
        );
    }

    #[test]
    fn error_codes_never_contain_the_token() {
        for error in [
            BridgeError::Unavailable,
            BridgeError::Timeout,
            BridgeError::Unauthorized,
            BridgeError::StepUpUnavailable,
            BridgeError::StepUpFailed,
            BridgeError::StepUpLocked,
            BridgeError::NotConfigured,
            BridgeError::TooLarge,
            BridgeError::Invalid,
            BridgeError::Failed,
        ] {
            assert!(!error.as_code().contains(TOKEN));
        }
    }

    #[test]
    fn timeouts_are_finite() {
        for route in Route::ALL {
            assert!(route.timeout() <= Duration::from_secs(120));
            assert!(route.timeout() > Duration::ZERO);
        }
    }
}
