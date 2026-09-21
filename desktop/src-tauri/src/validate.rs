//! Argument validation for the webview-facing commands. These are bounds and
//! shape checks only (defense in depth); the backend validates again and
//! remains the authority.

use crate::backend::BridgeError;

pub const MAX_MESSAGE_CHARS: usize = 100_000;
pub const MAX_SPEAK_CHARS: usize = 20_000;
pub const MAX_QUERY_CHARS: usize = 1_000;
pub const MAX_NAME_CHARS: usize = 200;
pub const MAX_ID_CHARS: usize = 100;
pub const MAX_DOCUMENT_BASE64: usize = 28_000_000;
pub const MAX_AUDIO_BASE64: usize = 11_500_000;

pub fn text(value: &str, max: usize) -> Result<(), BridgeError> {
    if value.trim().is_empty() || value.chars().count() > max {
        return Err(BridgeError::Invalid);
    }
    Ok(())
}

pub fn text_allow_empty(value: &str, max: usize) -> Result<(), BridgeError> {
    if value.chars().count() > max {
        return Err(BridgeError::Invalid);
    }
    Ok(())
}

pub fn id(value: &str) -> Result<(), BridgeError> {
    let ok = !value.is_empty()
        && value.len() <= MAX_ID_CHARS
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.' | b':'));
    if ok {
        Ok(())
    } else {
        Err(BridgeError::Invalid)
    }
}

pub fn optional_id(value: Option<&str>) -> Result<(), BridgeError> {
    value.map_or(Ok(()), id)
}

/// A user-typed step-up secret: bounded, never logged or echoed.
pub fn optional_step_up(value: Option<&str>) -> Result<(), BridgeError> {
    match value {
        None => Ok(()),
        Some(v) if !v.is_empty() && v.chars().count() <= 256 => Ok(()),
        Some(_) => Err(BridgeError::Invalid),
    }
}

/// The UI language preference: a closed set, never free text.
pub fn language(value: &str) -> Result<(), BridgeError> {
    match value {
        "auto" | "fa" | "en" => Ok(()),
        _ => Err(BridgeError::Invalid),
    }
}

/// The language of text to be spoken: only the two supported languages.
pub fn speech_language(value: Option<&str>) -> Result<(), BridgeError> {
    match value {
        None | Some("fa") | Some("en") => Ok(()),
        _ => Err(BridgeError::Invalid),
    }
}

/// Guest Mode duration in minutes (the backend enforces the same 1..=30).
pub fn guest_minutes(value: u32) -> Result<(), BridgeError> {
    if (1..=30).contains(&value) {
        Ok(())
    } else {
        Err(BridgeError::Invalid)
    }
}

/// A user-typed step-up secret that must be present (not optional).
pub fn required_step_up(value: &str) -> Result<(), BridgeError> {
    if !value.is_empty() && value.chars().count() <= 256 {
        Ok(())
    } else {
        Err(BridgeError::Invalid)
    }
}

/// A trusted provider id (never a provider name, model, or endpoint).
pub fn provider_id(value: &str) -> Result<(), BridgeError> {
    match value {
        "claude_subscription" | "gemini_free" | "openai_api" | "grok_api" => Ok(()),
        _ => Err(BridgeError::Invalid),
    }
}

/// The owner's report about Claude's "help improve" setting.
pub fn improvement_state(value: &str) -> Result<(), BridgeError> {
    match value {
        "unknown" | "owner_reports_disabled" | "owner_reports_enabled" => Ok(()),
        _ => Err(BridgeError::Invalid),
    }
}

/// The owner's session-only attestation about the Google project behind the
/// Gemini key. Sam cannot verify it; the backend requires step-up to loosen it.
pub fn gemini_attestation(value: &str) -> Result<(), BridgeError> {
    match value {
        "unknown" | "owner_attested_unbilled" => Ok(()),
        _ => Err(BridgeError::Invalid),
    }
}

/// How the owner marks a chat message. It can only raise how restricted a
/// request is; there is deliberately no "public" or "secret" value here.
pub fn privacy_label(value: &str) -> Result<(), BridgeError> {
    match value {
        "normal" | "personal" | "private" => Ok(()),
        _ => Err(BridgeError::Invalid),
    }
}

/// The owner's own topic blocklist: bounded, single-line, no control characters.
pub fn blocklist(items: &[String]) -> Result<(), BridgeError> {
    if items.len() > 64 {
        return Err(BridgeError::Invalid);
    }
    for item in items {
        let count = item.chars().count();
        if item.trim().is_empty() || count > 80 || item.chars().any(char::is_control) {
            return Err(BridgeError::Invalid);
        }
    }
    Ok(())
}

/// A voice *profile id* (never a provider voice reference or endpoint).
pub fn profile_id(value: &str) -> Result<(), BridgeError> {
    let ok = !value.is_empty()
        && value.len() <= 64
        && value
            .bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || matches!(b, b'_' | b'-'));
    if ok {
        Ok(())
    } else {
        Err(BridgeError::Invalid)
    }
}

pub fn resource_type(value: &str) -> Result<(), BridgeError> {
    match value {
        "pdf" | "txt" | "markdown" | "json" | "csv" => Ok(()),
        _ => Err(BridgeError::Invalid),
    }
}

pub fn base64_payload(value: &str, max: usize) -> Result<(), BridgeError> {
    let ok = !value.is_empty()
        && value.len() <= max
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'+' | b'/' | b'='));
    if ok {
        Ok(())
    } else {
        Err(BridgeError::Invalid)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn provider_ids_are_the_four_trusted_ones_only() {
        for ok in [
            "claude_subscription",
            "gemini_free",
            "openai_api",
            "grok_api",
        ] {
            assert!(provider_id(ok).is_ok());
        }
        for bad in [
            "",
            "anthropic_api",
            "gpt-5",
            "https://x",
            "Claude",
            "gemini_free ",
        ] {
            assert!(provider_id(bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn improvement_state_and_privacy_label_are_closed_sets() {
        for ok in ["unknown", "owner_reports_disabled", "owner_reports_enabled"] {
            assert!(improvement_state(ok).is_ok());
        }
        assert!(improvement_state("on").is_err());
        for ok in ["normal", "personal", "private"] {
            assert!(privacy_label(ok).is_ok());
        }
        for bad in ["", "public", "secret", "PRIVATE", "normal "] {
            assert!(privacy_label(bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn gemini_attestation_is_a_closed_set() {
        for ok in ["unknown", "owner_attested_unbilled"] {
            assert!(gemini_attestation(ok).is_ok());
        }
        for bad in ["", "free", "billed", "OWNER_ATTESTED_UNBILLED", "unknown "] {
            assert!(gemini_attestation(bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn blocklist_is_bounded_and_single_line() {
        assert!(blocklist(&[]).is_ok());
        assert!(blocklist(&["horse racing".to_string()]).is_ok());
        assert!(blocklist(&["".to_string()]).is_err());
        assert!(blocklist(&["x".repeat(81)]).is_err());
        assert!(blocklist(&["a\nb".to_string()]).is_err());
        assert!(blocklist(&vec!["x".to_string(); 65]).is_err());
    }

    #[test]
    fn text_bounds() {
        assert!(text("hello", 10).is_ok());
        assert!(text("   ", 10).is_err());
        assert!(text(&"a".repeat(11), 10).is_err());
        assert!(text_allow_empty("", 10).is_ok());
    }

    #[test]
    fn ids_reject_paths_urls_and_whitespace() {
        assert!(id("res-1_a:b.c").is_ok());
        for bad in [
            "",
            "../etc",
            "a/b",
            "a b",
            "http://x",
            "a?b=c",
            &"a".repeat(101),
        ] {
            assert!(id(bad).is_err(), "{bad}");
        }
        assert!(optional_id(None).is_ok());
        assert!(optional_id(Some("ok-1")).is_ok());
        assert!(optional_id(Some("no way")).is_err());
    }

    #[test]
    fn profile_ids_are_trusted_id_shape_only() {
        assert!(profile_id("sam_default").is_ok());
        for bad in [
            "",
            "Sam",
            "https://api.fish.audio",
            "abc/def",
            "voice ref",
            &"a".repeat(65),
        ] {
            assert!(profile_id(bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn language_and_guest_minutes_are_closed_sets() {
        for ok in ["auto", "fa", "en"] {
            assert!(language(ok).is_ok());
        }
        for bad in ["", "FA", "klingon", "fa-IR", "auto "] {
            assert!(language(bad).is_err(), "{bad}");
        }
        assert!(guest_minutes(1).is_ok() && guest_minutes(30).is_ok());
        assert!(guest_minutes(0).is_err() && guest_minutes(31).is_err());
        assert!(required_step_up("x").is_ok());
        assert!(required_step_up("").is_err());
        assert!(required_step_up(&"x".repeat(257)).is_err());
    }

    #[test]
    fn speech_language_is_fa_or_en_only() {
        assert!(speech_language(None).is_ok());
        assert!(speech_language(Some("fa")).is_ok() && speech_language(Some("en")).is_ok());
        for bad in ["auto", "", "FA", "fa-IR", "klingon"] {
            assert!(speech_language(Some(bad)).is_err(), "{bad}");
        }
    }

    #[test]
    fn step_up_is_bounded() {
        assert!(optional_step_up(None).is_ok());
        assert!(optional_step_up(Some("a-long-enough-secret")).is_ok());
        assert!(optional_step_up(Some("")).is_err());
        assert!(optional_step_up(Some(&"x".repeat(257))).is_err());
    }

    #[test]
    fn resource_types_are_an_allowlist() {
        for ok in ["pdf", "txt", "markdown", "json", "csv"] {
            assert!(resource_type(ok).is_ok());
        }
        for bad in ["exe", "PDF", "", "../pdf"] {
            assert!(resource_type(bad).is_err());
        }
    }

    #[test]
    fn base64_shape_and_size() {
        assert!(base64_payload("aGVsbG8=", 100).is_ok());
        assert!(base64_payload("", 100).is_err());
        assert!(base64_payload("a b", 100).is_err());
        assert!(base64_payload("data:audio/wav;base64,AAAA", 100).is_err());
        assert!(base64_payload("AAAA", 3).is_err());
    }
}
