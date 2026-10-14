/** Safe speech diagnostics. Provider bodies and arbitrary exceptions never reach captions. */
export function speechProblem(reason: string | null | undefined): string {
  switch (reason) {
    case "automatic_speech_off": return "Turn on Sam speaks responses in Settings.";
    case "external_speech_consent_required": return "Approve the displayed speech provider in Settings before text is sent for speech.";
    case "speech_language_unavailable":
    case "preview_language_unavailable": return "No trusted Sam voice is configured for this language. Choose a supported voice in Settings.";
    case "speech_content_withheld":
    case "private_speech_withheld": return "This response was kept as text to protect private content.";
    case "speech_permission_denied": return "Speech permission was revoked. Review Sam Voice in Settings.";
    case "speech_session_invalid": return "This voice preview expired. Try the preview again.";
    case "speech_profile_changed": return "Sam's selected voice changed. Try again.";
    case "local_voice_unavailable":
    case "speech_unavailable": return "The installed Sam voice couldn't be loaded. Check voices in macOS Settings.";
    default: return "Sam couldn't play speech. Check the sound output and try Preview Sam's voice.";
  }
}
