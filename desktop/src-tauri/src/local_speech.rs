//! On-device macOS speech. Only a fixed system executable and fixed options
//! are used; reply text is bounded stdin data, never argv, a file or a shell.

use std::collections::VecDeque;
use std::io::{Read, Write};
use std::process::{Child, Command, Stdio};
use std::sync::{mpsc, Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};

pub const MAX_TEXT_CHARS: usize = 8_000;
const MAX_CANCELLED: usize = 32;
const SPEECH_TIMEOUT: Duration = Duration::from_secs(600);
const POLL_INTERVAL: Duration = Duration::from_millis(100);
const CATALOG_TIMEOUT: Duration = Duration::from_secs(2);
const MAX_CATALOG_BYTES: usize = 262_144;
const MAX_CATALOG_VOICES: usize = 1_024;
const FAILED: &str = "speech_failed";
const UNAVAILABLE: &str = "speech_unavailable";
static INSTALLED_VOICES: OnceLock<Vec<SystemVoice>> = OnceLock::new();

#[derive(Debug, PartialEq)]
struct SystemVoice {
    name: String,
    language: String,
}

#[derive(Default)]
struct SpeechState {
    active: Option<(String, Child)>,
    cancelled: VecDeque<String>,
    exiting: bool,
}

#[derive(Clone, Default)]
pub struct LocalSpeech {
    inner: Arc<Mutex<SpeechState>>,
}

fn speech_text(text: &str) -> Result<String, String> {
    if text.trim().is_empty()
        || text.chars().count() > MAX_TEXT_CHARS
        || text
            .chars()
            .any(|c| c.is_control() && !matches!(c, '\n' | '\r' | '\t'))
    {
        return Err("invalid_request".to_string());
    }
    // Speech Synthesis Manager interprets [[...]] as control instructions.
    // Read brackets as punctuation; the reply must never control the engine.
    Ok(text.replace('[', "(").replace(']', ")"))
}

fn catalog_language(locale: &str) -> Option<&str> {
    if locale.len() > 32
        || !locale
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-'))
    {
        return None;
    }
    // Apple's Hong Kong voice is Cantonese, not the Mandarin voice. Locale
    // aliases here never widen the canonical language accepted over IPC.
    let language = if matches!(locale, "zh_HK" | "zh-HK") {
        "yue"
    } else {
        match locale.split(['_', '-']).next()? {
            "nb" => "no",
            "jv" => "jw",
            "fil" => "tl",
            "iw" => "he",
            other => other,
        }
    };
    crate::validate::speech_language(Some(language)).ok()?;
    Some(language)
}

fn parse_catalog(bytes: &[u8]) -> Vec<SystemVoice> {
    if bytes.len() > MAX_CATALOG_BYTES {
        return Vec::new();
    }
    let Ok(output) = std::str::from_utf8(bytes) else {
        return Vec::new();
    };
    output
        .lines()
        .take(MAX_CATALOG_VOICES)
        .filter_map(|line| {
            let header = line.split_once('#')?.0.trim();
            let (name, locale) = header.rsplit_once(char::is_whitespace)?;
            let name = name.trim();
            if name.is_empty()
                || name.starts_with('-')
                || name.chars().count() > 128
                || name.chars().any(char::is_control)
            {
                return None;
            }
            Some(SystemVoice {
                name: name.to_string(),
                language: catalog_language(locale)?.to_string(),
            })
        })
        .collect()
}

fn installed_voices() -> Vec<SystemVoice> {
    // Query only installed system voices. Fixed command, bounded stdout and
    // deadline; neither reply text nor model output can control this lookup.
    let Ok(mut child) = Command::new("/usr/bin/say")
        .args(["-v", "?"])
        .env_clear()
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
    else {
        return Vec::new();
    };
    let Some(stdout) = child.stdout.take() else {
        stop_child(&mut child);
        return Vec::new();
    };
    let deadline = Instant::now() + CATALOG_TIMEOUT;
    let (sender, receiver) = mpsc::sync_channel(1);
    std::thread::spawn(move || {
        let mut output = Vec::new();
        let result = stdout
            .take((MAX_CATALOG_BYTES + 1) as u64)
            .read_to_end(&mut output);
        let _ = sender.send(result.map(|_| output));
    });
    let output = match receiver.recv_timeout(CATALOG_TIMEOUT) {
        Ok(Ok(output)) if output.len() <= MAX_CATALOG_BYTES => output,
        _ => {
            stop_child(&mut child);
            return Vec::new();
        }
    };
    loop {
        match child.try_wait() {
            Ok(Some(status)) if status.success() => return parse_catalog(&output),
            Ok(Some(_)) => return Vec::new(),
            Ok(None) if Instant::now() < deadline => std::thread::sleep(POLL_INTERVAL),
            _ => {
                stop_child(&mut child);
                return Vec::new();
            }
        }
    }
}

fn select_voice<'a>(voices: &'a [SystemVoice], language: &str) -> Option<&'a str> {
    let matching = || voices.iter().filter(|voice| voice.language == language);
    // Prefer the regular voice over the parenthesized character variants.
    matching()
        .find(|voice| !voice.name.contains('('))
        .or_else(|| matching().next())
        .map(|voice| voice.name.as_str())
}

fn voice(language: &str) -> Result<String, String> {
    crate::validate::speech_language(Some(language)).map_err(|_| "invalid_request".to_string())?;
    if !cfg!(target_os = "macos") {
        return Err(UNAVAILABLE.to_string());
    }
    // Preserve the immediate English path. Never install voices or substitute
    // a different language; an unavailable voice uses the UI's provider flow.
    if language == "en" {
        return Ok("Samantha".to_string());
    }
    select_voice(INSTALLED_VOICES.get_or_init(installed_voices), language)
        .map(str::to_string)
        .ok_or_else(|| UNAVAILABLE.to_string())
}

fn stop_child(child: &mut Child) {
    let _ = child.kill();
    let _ = child.wait();
}

impl LocalSpeech {
    pub fn speak(&self, speech_id: &str, text: &str, language: &str) -> Result<(), String> {
        let text = speech_text(text)?;
        crate::validate::speech_language(Some(language))
            .map_err(|_| "invalid_request".to_string())?;
        {
            let state = self.inner.lock().map_err(|_| FAILED.to_string())?;
            if state.exiting || state.cancelled.iter().any(|id| id == speech_id) {
                return Ok(());
            }
        }
        let voice = voice(language)?;
        {
            let mut state = self.inner.lock().map_err(|_| FAILED.to_string())?;
            // A stop can reach IPC before the blocking speech worker starts.
            if state.exiting || state.cancelled.iter().any(|id| id == speech_id) {
                return Ok(());
            }
            if let Some((_, mut previous)) = state.active.take() {
                stop_child(&mut previous);
            }
            let mut child = Command::new("/usr/bin/say")
                .args(["-v", voice.as_str(), "-r", "180", "-f", "-"])
                .env_clear()
                .stdin(Stdio::piped())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .spawn()
                .map_err(|_| UNAVAILABLE.to_string())?;
            let wrote = child
                .stdin
                .take()
                .ok_or(())
                .and_then(|mut stdin| stdin.write_all(text.as_bytes()).map_err(|_| ()));
            if wrote.is_err() {
                stop_child(&mut child);
                return Err(FAILED.to_string());
            }
            state.active = Some((speech_id.to_string(), child));
        }
        let deadline = Instant::now() + SPEECH_TIMEOUT;
        loop {
            {
                let mut state = self.inner.lock().map_err(|_| FAILED.to_string())?;
                let Some((id, child)) = state.active.as_mut() else {
                    return Ok(()); // explicitly stopped
                };
                if id != speech_id {
                    return Ok(()); // superseded by a newer reply
                }
                match child.try_wait() {
                    Ok(Some(status)) => {
                        state.active = None;
                        return if status.success() {
                            Ok(())
                        } else {
                            Err(FAILED.to_string())
                        };
                    }
                    Ok(None) if Instant::now() < deadline => {}
                    _ => {
                        stop_child(child);
                        state.active = None;
                        return Err(FAILED.to_string());
                    }
                }
            }
            std::thread::sleep(POLL_INTERVAL);
        }
    }

    pub fn stop(&self, speech_id: &str) {
        if let Ok(mut state) = self.inner.lock() {
            if state.active.as_ref().is_some_and(|(id, _)| id == speech_id) {
                if let Some((_, mut child)) = state.active.take() {
                    stop_child(&mut child);
                }
            }
            if !state.cancelled.iter().any(|id| id == speech_id) {
                state.cancelled.push_back(speech_id.to_string());
            }
            while state.cancelled.len() > MAX_CANCELLED {
                state.cancelled.pop_front();
            }
        }
    }

    pub fn stop_all(&self) {
        if let Ok(mut state) = self.inner.lock() {
            state.exiting = true;
            if let Some((_, mut child)) = state.active.take() {
                stop_child(&mut child);
            }
        }
    }
}

impl Drop for SpeechState {
    fn drop(&mut self) {
        if let Some((_, mut child)) = self.active.take() {
            stop_child(&mut child);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bounds_and_speech_controls_are_checked_before_spawning() {
        assert!(speech_text("").is_err());
        assert!(speech_text(" \n").is_err());
        assert!(speech_text("\0reply").is_err());
        assert!(speech_text(&"x".repeat(MAX_TEXT_CHARS + 1)).is_err());
        assert_eq!(
            speech_text("Hello [[slnc 999999]] [[volm 0]]").unwrap(),
            "Hello ((slnc 999999)) ((volm 0))"
        );
        assert_eq!(speech_text("سلام\nSam").unwrap(), "سلام\nSam");
    }

    #[test]
    fn only_supported_languages_and_installed_voice_are_selectable() {
        assert_eq!(voice("-o /tmp/output"), Err("invalid_request".to_string()));
        if cfg!(target_os = "macos") {
            assert_eq!(voice("en"), Ok("Samantha".to_string()));
        }
    }

    #[test]
    fn installed_catalog_matches_language_and_prefers_regular_voices() {
        let voices = parse_catalog(
            concat!(
                "Eddy (French (France))  fr_FR # Bonjour\n",
                "Thomas                  fr_FR # Bonjour\n",
                "Mónica                  es_ES # Hola\n",
                "Majed                   ar_001 # Hello\n",
                "Sinji                   zh_HK # Hello\n",
                "Tingting                zh_CN # Hello\n",
                "Nora                    nb_NO # Hei\n",
                "Hebrew voice            iw_IL # Hello\n",
                "Javanese voice          jv_ID # Hello\n",
                "Tagalog voice           fil_PH # Hello\n"
            )
            .as_bytes(),
        );
        assert_eq!(select_voice(&voices, "fr"), Some("Thomas"));
        assert_eq!(select_voice(&voices, "es"), Some("Mónica"));
        assert_eq!(select_voice(&voices, "ar"), Some("Majed"));
        assert_eq!(select_voice(&voices, "fa"), None);
        assert_eq!(select_voice(&voices, "de"), None);
        assert_eq!(select_voice(&voices, "zh"), Some("Tingting"));
        assert_eq!(select_voice(&voices, "yue"), Some("Sinji"));
        assert_eq!(select_voice(&voices, "no"), Some("Nora"));
        assert_eq!(select_voice(&voices, "he"), Some("Hebrew voice"));
        assert_eq!(select_voice(&voices, "jw"), Some("Javanese voice"));
        assert_eq!(select_voice(&voices, "tl"), Some("Tagalog voice"));
    }

    #[test]
    fn malformed_catalog_cannot_select_a_voice_or_exceed_bounds() {
        for malformed in [
            "-v /tmp/voice fr_FR # Hello",
            "bad\0name fr_FR # Hello",
            "Thomas unknown # Hello",
            "Thomas fr_FR/../../tmp # Hello",
            "Thomas fr_FR",
        ] {
            assert!(parse_catalog(malformed.as_bytes()).is_empty());
        }
        assert!(parse_catalog(&vec![b'x'; MAX_CATALOG_BYTES + 1]).is_empty());
        assert!(parse_catalog(&[0xff]).is_empty());
        let many = "Thomas fr_FR # Hello\n".repeat(MAX_CATALOG_VOICES + 1);
        assert_eq!(parse_catalog(many.as_bytes()).len(), MAX_CATALOG_VOICES);
    }

    #[test]
    fn cancellation_before_worker_start_does_not_spawn() {
        let speech = LocalSpeech::default();
        speech.stop("cancelled");
        if cfg!(target_os = "macos") {
            assert_eq!(speech.speak("cancelled", "Never speak this", "en"), Ok(()));
        }
        assert_eq!(
            speech.speak("cancelled", "Ne jamais prononcer", "fr"),
            Ok(())
        );
        assert!(speech.inner.lock().unwrap().active.is_none());
    }

    #[test]
    fn cancel_history_is_bounded_and_keeps_recent_requests() {
        let speech = LocalSpeech::default();
        for index in 0..100 {
            speech.stop(&index.to_string());
        }
        let state = speech.inner.lock().unwrap();
        assert_eq!(state.cancelled.len(), MAX_CANCELLED);
        assert_eq!(state.cancelled.back().map(String::as_str), Some("99"));
    }
}
