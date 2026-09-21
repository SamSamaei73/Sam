import { useCallback, useRef, useState } from "react";
import { BridgeError, toBridgeError } from "../bridge/bridge";
import type { OperationResult, ResponseLanguage } from "../bridge/types";
import { AgentActivity } from "../components/AgentActivity";
import { Composer } from "../components/Composer";
import { ConnectionIndicator } from "../components/ConnectionIndicator";
import { IconKnowledge, IconPanelRight, IconPlus, IconShield, IconSparkle } from "../components/Icons";
import { MessageBubble, type ChatMessage } from "../components/MessageBubble";
import { IconButton, NeonButton, Notice, SelectPill } from "../components/primitives";
import { ReadAloud } from "../components/ReadAloud";
import { VoiceRecorder } from "../components/VoiceRecorder";
import { detectLanguage } from "../lib/language";
import type { AudioEnvironment } from "../lib/recorder";
import { useSam } from "../state";

let counter = 0;
const nextId = () => `m${++counter}`;

function failureText(error: unknown): string {
  return (error instanceof BridgeError ? error : toBridgeError(error)).message;
}

function resultText(result: OperationResult): string {
  return result.message ?? "That couldn't be completed.";
}

export function SamView({
  messages,
  setMessages,
  conversations,
  activeConversation,
  onSelectConversation,
  onNewChat,
  panelOpen,
  onTogglePanel,
  onNavigate,
  audioEnvironment,
}: {
  messages: ChatMessage[];
  setMessages: (update: React.SetStateAction<ChatMessage[]>) => void;
  conversations: { id: string; title: string }[];
  activeConversation: string;
  onSelectConversation: (id: string) => void;
  onNewChat: () => void;
  panelOpen: boolean;
  onTogglePanel: () => void;
  onNavigate: (view: "knowledge" | "permissions") => void;
  audioEnvironment?: AudioEnvironment | null;
}) {
  const { bridge, status, connection, prefs, confirmable, t, refreshIdentity } = useSam();
  const [busy, setBusy] = useState<string | null>(null);
  // The owner can mark a message private. It only ever RAISES how restricted
  // the request is; the backend still detects secrets on its own.
  const [privateChat, setPrivateChat] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const endRef = useRef<HTMLDivElement>(null);

  const push = useCallback(
    (message: Omit<ChatMessage, "id">) => {
      setMessages((current) => [...current, { id: nextId(), ...message }]);
      window.setTimeout(() => endRef.current?.scrollIntoView?.({ block: "end" }), 0);
    },
    [setMessages],
  );

  const agentReady = status?.agent === "configured";
  const profiles = status?.speech_profiles ?? [];
  /** A trusted voice that can actually speak this language, or none (text-only). */
  const profileFor = (language: ResponseLanguage): string | null => {
    const capable = profiles.filter((p) => p.languages.includes(language));
    const preferred = capable.find((p) => p.profile_id === prefs.readAloudVoice);
    return (preferred ?? capable[0])?.profile_id ?? null;
  };
  const canSpeak = status?.speech_output === "configured";

  const send = async (text: string) => {
    setError(null);
    push({ role: "user", text });
    setBusy("Sam is thinking…");
    try {
      const result = await bridge.chat(text, prefs.responseLanguage, privateChat ? "private" : "normal");
      if (result.status === "ok" && result.reply !== null) {
        push({
          role: "sam",
          text: result.reply,
          status: "ok",
          referenceId: result.reference_id,
          language: result.language,
          direction: result.direction,
        });
      } else {
        push({ role: "notice", text: resultText(result), status: "failed", referenceId: result.reference_id });
      }
    } catch (failure) {
      push({ role: "notice", text: failureText(failure), status: "failed" });
    } finally {
      setBusy(null);
    }
  };

  const sendVoice = async (audioBase64: string) => {
    setError(null);
    setBusy("Understanding your voice message…");
    try {
      const result = await confirmable((confirmationId) =>
        bridge.voiceUtterance(audioBase64, confirmationId, prefs.responseLanguage),
      );
      void refreshIdentity();
      if (result.reason_code === "owner_verification_required") {
        // A non-owner speaker is stopped before any transcription: show the
        // fixed, translated notice — never a transcript.
        push({ role: "notice", text: t("voice.ownerRequired"), status: "failed" });
      } else if (result.status === "ok" && result.transcript) {
        push({
          role: result.speaker === "guest" ? "guest" : "user",
          text: result.transcript,
          source: "voice",
          language: detectLanguage(result.transcript),
        });
        if (result.reply) {
          push({
            role: "sam",
            text: result.reply,
            status: "ok",
            language: result.language,
            direction: result.direction,
          });
        }
      } else {
        // Secret-looking or otherwise ineligible transcripts are never shown or forwarded.
        push({ role: "notice", text: resultText(result), status: "failed", referenceId: result.reference_id });
      }
    } catch (failure) {
      push({ role: "notice", text: failureText(failure), status: "failed" });
    } finally {
      setBusy(null);
    }
  };

  const hour = new Date().getHours();
  const greeting = t(
    hour < 12 ? "chat.greeting.morning" : hour < 18 ? "chat.greeting.afternoon" : "chat.greeting.evening",
  );

  return (
    <section className="chat" aria-label="Conversation with Sam">
      <div className="chat-toolbar">
        <ConnectionIndicator state={connection} />
        {messages.length > 0 ? (
          <NeonButton variant="quiet" onClick={() => setMessages([])}>
            {t("chat.clear")}
          </NeonButton>
        ) : null}
        <IconButton
          label={panelOpen ? "Hide system panel" : "Show system panel"}
          selected={panelOpen}
          onClick={onTogglePanel}
        >
          <IconPanelRight />
        </IconButton>
      </div>
      <div className="thread" role="log" aria-live="polite" aria-relevant="additions">
        <div className="thread-inner">
          {messages.length === 0 ? (
            <div className="hero">
              <div className="hero-tile" aria-hidden="true">
                <IconSparkle />
              </div>
              <h2>{greeting}</h2>
              <p>{t("chat.hero")}</p>
              <div className="chips">
                <button type="button" className="chip" onClick={() => onNavigate("knowledge")}>
                  <IconKnowledge /> {t("chat.addDocument")}
                </button>
                <button type="button" className="chip" onClick={() => onNavigate("permissions")}>
                  <IconShield /> {t("chat.reviewPermissions")}
                </button>
              </div>
            </div>
          ) : (
            messages.map((message) => {
              const language = message.language ?? detectLanguage(message.text) ?? "en";
              const profile = canSpeak && message.role === "sam" ? profileFor(language) : null;
              return (
                <MessageBubble
                  key={message.id}
                  message={message}
                  tools={
                    profile ? (
                      <ReadAloud
                        bridge={bridge}
                        text={message.text}
                        profile={profile}
                        language={language}
                        confirm={confirmable}
                        onError={setError}
                      />
                    ) : null
                  }
                />
              );
            })
          )}
          <div ref={endRef} />
        </div>
      </div>
      {connection === "unavailable" ? (
        <div className="composer-wrap" style={{ paddingBottom: 10 }}>
          <Notice tone="danger">Sam's backend isn't reachable, so messages can't be sent right now.</Notice>
        </div>
      ) : status && !agentReady ? (
        <div className="composer-wrap" style={{ paddingBottom: 10 }}>
          <Notice tone="warn">Sam's language model isn't configured, so chat is unavailable.</Notice>
        </div>
      ) : null}
      {error ? (
        <div className="composer-wrap" style={{ paddingBottom: 10 }}>
          <Notice tone="danger">{error}</Notice>
        </div>
      ) : null}
      {busy ? <AgentActivity label={busy} /> : null}
      <div className="composer-wrap">
        <div className="composer-top">
          <SelectPill
            label="Conversation"
            value={activeConversation}
            onChange={onSelectConversation}
            options={conversations.map((c) => ({ value: c.id, label: c.title }))}
          />
          <IconButton label={t("chat.newChat")} onClick={onNewChat}>
            <IconPlus />
          </IconButton>
        </div>
        <Composer
          disabled={busy !== null || !agentReady || connection === "unavailable"}
          onSend={(text) => void send(text)}
          leading={
            <>
              <span className="pill" data-tone="info">
                {t("chat.sam")}
              </span>
              <button
                type="button"
                className="pill"
                data-tone={privateChat ? "warn" : "muted"}
                aria-pressed={privateChat}
                title={t("models.privateChatHint")}
                onClick={() => setPrivateChat(!privateChat)}
              >
                {t("models.privateChat")}
              </button>
            </>
          }
          trailing={
            <VoiceRecorder
              available={status?.voice_input === "configured"}
              busy={busy !== null}
              environment={audioEnvironment}
              onRecorded={(audio) => void sendVoice(audio)}
              onError={setError}
            />
          }
        />
      </div>
    </section>
  );
}
