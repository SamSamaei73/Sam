import { useCallback, useRef, useState } from "react";
import { BridgeError, toBridgeError } from "../bridge/bridge";
import type { OperationResult } from "../bridge/types";
import { AgentActivity } from "../components/AgentActivity";
import { ConnectionIndicator } from "../components/ConnectionIndicator";
import { IconKnowledge, IconPanelRight, IconPlus, IconShield, IconSparkle } from "../components/Icons";
import { Composer } from "../components/Composer";
import { MessageBubble, type ChatMessage } from "../components/MessageBubble";
import { IconButton, NeonButton, Notice, SelectPill } from "../components/primitives";
import { ReadAloud } from "../components/ReadAloud";
import { VoiceRecorder } from "../components/VoiceRecorder";
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
  const { bridge, status, connection, prefs, confirmable } = useSam();
  const [busy, setBusy] = useState<string | null>(null);
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
  const activeProfile =
    profiles.find((p) => p.profile_id === prefs.readAloudVoice)?.profile_id ?? profiles[0]?.profile_id ?? null;
  const canSpeak = status?.speech_output === "configured" && activeProfile !== null;

  const send = async (text: string) => {
    setError(null);
    push({ role: "user", text });
    setBusy("Sam is thinking…");
    try {
      const result = await bridge.chat(text);
      if (result.status === "ok" && result.reply !== null) {
        push({ role: "sam", text: result.reply, status: "ok", referenceId: result.reference_id });
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
      const result = await confirmable((confirmationId) => bridge.voiceUtterance(audioBase64, confirmationId));
      if (result.status === "ok" && result.transcript) {
        push({ role: "user", text: result.transcript, source: "voice" });
        if (result.reply) push({ role: "sam", text: result.reply, status: "ok" });
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

  const greeting = (() => {
    const hour = new Date().getHours();
    return hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";
  })();

  return (
    <section className="chat" aria-label="Conversation with Sam">
      <div className="chat-toolbar">
        <ConnectionIndicator state={connection} />
        {messages.length > 0 ? (
          <NeonButton variant="quiet" onClick={() => setMessages([])}>
            Clear chat
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
              <p>
                Ask a question, search your documents, or use the mic to talk. This conversation lives only in this
                window and is never saved.
              </p>
              <div className="chips">
                <button type="button" className="chip" onClick={() => onNavigate("knowledge")}>
                  <IconKnowledge /> Add a document
                </button>
                <button type="button" className="chip" onClick={() => onNavigate("permissions")}>
                  <IconShield /> Review permissions
                </button>
              </div>
            </div>
          ) : (
            messages.map((message) => (
              <MessageBubble
                key={message.id}
                message={message}
                tools={
                  message.role === "sam" && canSpeak && activeProfile ? (
                    <ReadAloud
                      bridge={bridge}
                      text={message.text}
                      profile={activeProfile}
                      confirm={confirmable}
                      onError={setError}
                    />
                  ) : null
                }
              />
            ))
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
          <IconButton label="New chat" onClick={onNewChat}>
            <IconPlus />
          </IconButton>
        </div>
        <Composer
          disabled={busy !== null || !agentReady || connection === "unavailable"}
          onSend={(text) => void send(text)}
          leading={<span className="pill" data-tone="info">Sam</span>}
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
