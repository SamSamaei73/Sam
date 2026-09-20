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
