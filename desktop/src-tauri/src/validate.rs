//! Argument validation for the webview-facing commands. These are bounds and
//! shape checks only (defense in depth); the backend validates again and
//! remains the authority.

use crate::backend::BridgeError;

pub const MAX_MESSAGE_CHARS: usize = 100_000;
pub const MAX_SPEAK_CHARS: usize = 20_000;
pub const MAX_QUERY_CHARS: usize = 1_000;
/// Matches the backend's professional query bound.
pub const MAX_PROFESSIONAL_QUERY_CHARS: usize = 300;
pub const MAX_NAME_CHARS: usize = 200;
/// Matches the backend's proactive bounds.
pub const MAX_TASK_TITLE_CHARS: usize = 200;
pub const MAX_TASK_INSTRUCTION_CHARS: usize = 2_000;
const MAX_CONDITION_PARAMS: usize = 8;
const MAX_CONDITION_PARAM_CHARS: usize = 200;
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

/// The closed set of Professional Intelligence source types (never a path or URL).
pub fn professional_source_type(value: &str) -> Result<(), BridgeError> {
    match value {
        "master_cv"
        | "publication"
        | "transcript"
        | "project_documentation"
        | "github"
        | "linkedin_export"
        | "owner_document" => Ok(()),
        _ => Err(BridgeError::Invalid),
    }
}

/// The privacy class the owner selects for a professional source. There is
/// deliberately no "secret" (never ingested) and no "normal" value here.
pub fn professional_privacy(value: &str) -> Result<(), BridgeError> {
    match value {
        "public" | "personal" | "private" => Ok(()),
        _ => Err(BridgeError::Invalid),
    }
}

pub fn optional_professional_privacy(value: Option<&str>) -> Result<(), BridgeError> {
    value.map_or(Ok(()), professional_privacy)
}

/// The owner's review actions. There is no "verify" or "delete" here: verifying
/// is not something the UI can assert, and removal is its own confirmed command.
pub fn professional_review_action(value: &str) -> Result<(), BridgeError> {
    match value {
        "confirm" | "reject" | "resolve" | "set_privacy" => Ok(()),
        _ => Err(BridgeError::Invalid),
    }
}

pub fn professional_query_mode(value: &str) -> Result<(), BridgeError> {
    match value {
        "search" | "evidence_for" => Ok(()),
        _ => Err(BridgeError::Invalid),
    }
}

// ------------------------------------------------------------ proactive

fn one_of(value: &str, allowed: &[&str]) -> Result<(), BridgeError> {
    if allowed.contains(&value) {
        Ok(())
    } else {
        Err(BridgeError::Invalid)
    }
}

pub fn proactive_task_type(value: &str) -> Result<(), BridgeError> {
    one_of(value, &["one_time", "recurring", "condition_watch"])
}

pub fn proactive_timing(value: &str) -> Result<(), BridgeError> {
    one_of(
        value,
        &["exact_schedule", "flexible_schedule", "condition_watch"],
    )
}

/// What a run may do: a closed set. There is no "shell", "script" or "tool".
pub fn proactive_action(value: &str) -> Result<(), BridgeError> {
    one_of(value, &["reminder", "summary", "watch"])
}

/// There is deliberately no minutely or secondly frequency.
pub fn proactive_frequency(value: &str) -> Result<(), BridgeError> {
    one_of(value, &["none", "hourly", "daily", "weekly"])
}

pub fn optional_daypart(value: Option<&str>) -> Result<(), BridgeError> {
    value.map_or(Ok(()), |v| one_of(v, &["morning", "afternoon", "evening"]))
}

pub fn proactive_semantics(value: &str) -> Result<(), BridgeError> {
    one_of(value, &["becomes_true", "on_change", "repeat_while_true"])
}

pub fn proactive_level(value: &str) -> Result<(), BridgeError> {
    one_of(value, &["silent", "notify_owner", "requires_attention"])
}

pub fn optional_proactive_level(value: Option<&str>) -> Result<(), BridgeError> {
    value.map_or(Ok(()), proactive_level)
}

/// A suggested next action is a label for the owner, never something to run.
pub fn proactive_proposed(value: &str) -> Result<(), BridgeError> {
    one_of(
        value,
        &[
            "none",
            "review_in_sam",
            "open_professional",
            "open_model_settings",
            "review_deadline",
        ],
    )
}

/// The inbox can only mark read or dismiss: it never executes anything.
pub fn notification_action(value: &str) -> Result<(), BridgeError> {
    one_of(value, &["read", "dismiss"])
}

/// An IANA timezone name shape (the backend checks it exists).
pub fn timezone(value: &str) -> Result<(), BridgeError> {
    let ok = !value.is_empty()
        && value.len() <= 64
        && !value.contains("..")
        && !value.starts_with('/')
        && value
            .bytes()
            .next()
            .is_some_and(|b| b.is_ascii_alphabetic())
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'+' | b'-' | b'/'));
    if ok {
        Ok(())
    } else {
        Err(BridgeError::Invalid)
    }
}

/// YYYY-MM-DD.
pub fn iso_date(value: &str) -> Result<(), BridgeError> {
    let bytes = value.as_bytes();
    let ok = bytes.len() == 10
        && bytes[4] == b'-'
        && bytes[7] == b'-'
        && bytes
            .iter()
            .enumerate()
            .all(|(i, b)| i == 4 || i == 7 || b.is_ascii_digit());
    if ok {
        Ok(())
    } else {
        Err(BridgeError::Invalid)
    }
}

pub fn optional_iso_date(value: Option<&str>) -> Result<(), BridgeError> {
    value.map_or(Ok(()), iso_date)
}

/// HH:MM, minute precision.
pub fn optional_time_of_day(value: Option<&str>) -> Result<(), BridgeError> {
    let Some(value) = value else { return Ok(()) };
    let bytes = value.as_bytes();
    let digits = |a: u8, b: u8| u32::from(a - b'0') * 10 + u32::from(b - b'0');
    let ok = bytes.len() == 5
        && bytes[2] == b':'
        && [0, 1, 3, 4].iter().all(|&i| bytes[i].is_ascii_digit())
        && digits(bytes[0], bytes[1]) < 24
        && digits(bytes[3], bytes[4]) < 60;
    if ok {
        Ok(())
    } else {
        Err(BridgeError::Invalid)
    }
}

pub fn bounded_number(value: u32, min: u32, max: u32) -> Result<(), BridgeError> {
    if (min..=max).contains(&value) {
        Ok(())
    } else {
        Err(BridgeError::Invalid)
    }
}

pub fn weekdays(values: &[u8]) -> Result<(), BridgeError> {
    let mut seen = [false; 7];
    if values.len() > 7 {
        return Err(BridgeError::Invalid);
    }
    for &day in values {
        if day > 6 || seen[usize::from(day)] {
            return Err(BridgeError::Invalid);
        }
        seen[usize::from(day)] = true;
    }
    Ok(())
}

/// A trusted condition id: a simple identifier, never a path or module.
pub fn optional_condition_id(value: Option<&str>) -> Result<(), BridgeError> {
    let Some(value) = value else { return Ok(()) };
    let ok = (2..=48).contains(&value.len())
        && value.bytes().next().is_some_and(|b| b.is_ascii_lowercase())
        && value
            .bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_');
    if ok {
        Ok(())
    } else {
        Err(BridgeError::Invalid)
    }
}

pub fn condition_params(
    params: &std::collections::BTreeMap<String, String>,
) -> Result<(), BridgeError> {
    if params.len() > MAX_CONDITION_PARAMS {
        return Err(BridgeError::Invalid);
    }
    for (key, value) in params {
        let key_ok = (1..=32).contains(&key.len())
            && key.bytes().next().is_some_and(|b| b.is_ascii_lowercase())
            && key
                .bytes()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_');
        let value_ok = value.chars().count() <= MAX_CONDITION_PARAM_CHARS
            && !value.chars().any(char::is_control);
        if !key_ok || !value_ok {
            return Err(BridgeError::Invalid);
        }
    }
    Ok(())
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
    fn professional_closed_sets_reject_everything_else() {
        for ok in [
            "master_cv",
            "publication",
            "transcript",
            "project_documentation",
            "github",
            "linkedin_export",
            "owner_document",
        ] {
            assert!(professional_source_type(ok).is_ok(), "{ok}");
        }
        for bad in [
            "",
            "MASTER_CV",
            "cv",
            "../cv",
            "file:///etc/passwd",
            "arbitrary_file",
        ] {
            assert_eq!(
                professional_source_type(bad),
                Err(BridgeError::Invalid),
                "{bad}"
            );
        }
        for ok in ["public", "personal", "private"] {
            assert!(professional_privacy(ok).is_ok());
        }
        for bad in ["secret", "normal", "PUBLIC", "", "restricted"] {
            assert_eq!(
                professional_privacy(bad),
                Err(BridgeError::Invalid),
                "{bad}"
            );
        }
        assert!(optional_professional_privacy(None).is_ok());
        assert!(optional_professional_privacy(Some("private")).is_ok());
        assert_eq!(
            optional_professional_privacy(Some("secret")),
            Err(BridgeError::Invalid)
        );
        for ok in ["confirm", "reject", "resolve", "set_privacy"] {
            assert!(professional_review_action(ok).is_ok());
        }
        for bad in ["verify", "delete", "remove", "approve", "", "Confirm"] {
            assert_eq!(
                professional_review_action(bad),
                Err(BridgeError::Invalid),
                "{bad}"
            );
        }
        assert!(professional_query_mode("search").is_ok());
        assert!(professional_query_mode("evidence_for").is_ok());
        for bad in ["sql", "path", "", "SEARCH", "evidence"] {
            assert_eq!(
                professional_query_mode(bad),
                Err(BridgeError::Invalid),
                "{bad}"
            );
        }
    }

    #[test]
    fn proactive_closed_sets_reject_everything_else() {
        assert!(proactive_task_type("recurring").is_ok());
        assert!(proactive_timing("flexible_schedule").is_ok());
        assert!(proactive_action("watch").is_ok());
        for bad in ["shell", "script", "tool", "execute", "", "REMINDER"] {
            assert_eq!(proactive_action(bad), Err(BridgeError::Invalid), "{bad}");
        }
        for bad in ["minutely", "secondly", "* * * * *", "FREQ=SECONDLY", ""] {
            assert_eq!(proactive_frequency(bad), Err(BridgeError::Invalid), "{bad}");
        }
        assert!(optional_daypart(None).is_ok());
        assert_eq!(optional_daypart(Some("night")), Err(BridgeError::Invalid));
        assert!(proactive_semantics("on_change").is_ok());
        assert!(proactive_level("requires_attention").is_ok());
        assert!(proactive_proposed("review_deadline").is_ok());
        for bad in ["send_email", "apply_for_job", "delete", "run"] {
            assert_eq!(proactive_proposed(bad), Err(BridgeError::Invalid), "{bad}");
        }
        assert!(notification_action("dismiss").is_ok());
        for bad in ["execute", "approve", "run", ""] {
            assert_eq!(notification_action(bad), Err(BridgeError::Invalid), "{bad}");
        }
    }

    #[test]
    fn proactive_shapes_are_bounded() {
        for ok in [
            "Europe/London",
            "UTC",
            "America/Argentina/Buenos_Aires",
            "Etc/GMT+3",
        ] {
            assert!(timezone(ok).is_ok(), "{ok}");
        }
        for bad in [
            "",
            "../etc/passwd",
            "/etc/localtime",
            "Europe/../x",
            "a b",
            "9Zone",
        ] {
            assert_eq!(timezone(bad), Err(BridgeError::Invalid), "{bad}");
        }
        assert!(iso_date("2026-03-29").is_ok());
        for bad in ["2026-3-29", "tomorrow", "2026/03/29", ""] {
            assert_eq!(iso_date(bad), Err(BridgeError::Invalid), "{bad}");
        }
        assert!(optional_time_of_day(Some("23:59")).is_ok());
        for bad in ["24:00", "10:60", "10:00:30", "1000", "ab:cd"] {
            assert_eq!(
                optional_time_of_day(Some(bad)),
                Err(BridgeError::Invalid),
                "{bad}"
            );
        }
        assert!(weekdays(&[0, 2, 6]).is_ok());
        assert_eq!(weekdays(&[7]), Err(BridgeError::Invalid));
        assert_eq!(weekdays(&[1, 1]), Err(BridgeError::Invalid));
        assert!(bounded_number(1, 1, 365).is_ok());
        assert_eq!(bounded_number(0, 1, 365), Err(BridgeError::Invalid));
        assert!(optional_condition_id(Some("deadline_approaching")).is_ok());
        for bad in ["os.system", "subprocess:run", "../x", "Flag", "x"] {
            assert_eq!(
                optional_condition_id(Some(bad)),
                Err(BridgeError::Invalid),
                "{bad}"
            );
        }
        let mut params = std::collections::BTreeMap::new();
        params.insert("date".to_string(), "2026-01-07".to_string());
        assert!(condition_params(&params).is_ok());
        params.insert("Bad-Key".to_string(), "x".to_string());
        assert_eq!(condition_params(&params), Err(BridgeError::Invalid));
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
