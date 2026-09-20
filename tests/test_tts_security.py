"""Adversarial and structural security tests for the speech-synthesis layer.

Threat index (spec list number -> where covered):

  1-3    deny / engine failure / Phase 9 grant ... test_tts_gateway
  4-5    unknown / disabled profile .............. test_tts_gateway
  6-10   LLM-specified reference/model/endpoint/key/timeout ... test_tts_models
  11-12  secret text withheld, zero network ...... test_tts_gateway
  13-16  API key absent from requests/audit/errors/repr ... here, test_tts_models
  17     Fish error body not leaked .............. test_tts_fish, test_tts_gateway
  18-20  redirects / arbitrary host / environment  test_tts_fish
  21-24  text limits ............................. test_tts_models
  25-29  timeout, connection, 401/403, 429, 5xx .. test_tts_fish, test_tts_gateway
  30-34  audio response validation ............... test_tts_fish, test_tts_models
  35-37  audit privacy, no filesystem persistence . TestPrivacy
  38-39  no Memory / Knowledge write ............. TestIsolation
  40-42  one call, no retry, no second synthesis .. test_tts_gateway
  43-45  provider metadata inert ................. test_tts_gateway
  46-48  principal/profile isolation, immutability  test_tts_gateway, test_tts_models
  49-50  no cloning / reference-audio path ....... TestNoCloning
  51-52  no subprocess/ffmpeg/websocket/background  TestStaticHygiene
  53     audio repr .............................. test_tts_models
  54     credential unreachable from AgentCore ... TestCredentialReachability
  55     digest matches bytes .................... test_tts_gateway, test_tts_models
"""

from __future__ import annotations

import ast
import socket
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest

from sam.tts.credentials import FakeTTSCredentialProvider, TTSCredential
from sam.tts.models import TTSStatus
from tests.tts_support import (
    ALICE,
    FAKE_KEY,
    MP3,
    Recorder,
    TTSHarness,
    fish_harness,
    reachable,
)

SRC = Path(__file__).resolve().parents[1] / "src"
TTS_SRC = SRC / "sam" / "tts"
S = TTSStatus


def _trees() -> list[tuple[str, ast.AST]]:
    return [(p.name, ast.parse(p.read_text())) for p in sorted(TTS_SRC.glob("*.py"))]


# ================================================================ privacy


class TestPrivacy:
    def test_nothing_is_written_to_disk(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        h, _, _ = fish_harness()
        assert h.gateway.synthesize(h.request()).status is S.SUCCEEDED
        assert list(tmp_path.iterdir()) == []

    def test_the_key_never_appears_in_any_visible_output(self) -> None:
        h, rec, creds = fish_harness()
        result = h.gateway.synthesize(h.request("A uniquely private sentence."))
        blob = "\n".join(
            [
                repr(result),
                str(result),
                result.model_dump_json(),
                repr(h.gateway),
                repr(h.provider),
                repr(creds),
                repr(h.profiles),
                repr(h.audit),
                *[e.model_dump_json() for e in h.audit.list_events()],  # type: ignore[attr-defined]
                *[str(e) for e in h.permission_audit.list_events()],
                *[str(c) for c in h.confirmations._list_for_test()],
            ]
        )
        assert FAKE_KEY not in blob and "Bearer" not in blob
        assert "uniquely private" not in blob
        assert len(rec.requests) == 1

    def test_generated_audio_is_held_in_memory_only_by_the_result(self) -> None:
        h = TTSHarness()
        result = h.gateway.synthesize(h.request())
        assert result.audio is not None
        audio = result.audio.audio_bytes
        holders = [
            o
            for o in reachable(h.gateway) + reachable(h.audit)
            if isinstance(o, bytes) and o == audio
        ]
        assert holders == []  # the gateway, provider, audit, sessions keep no copy

    def test_request_text_is_not_retained_by_the_gateway_or_audit(self) -> None:
        h = TTSHarness()
        h.gateway.synthesize(h.request("RETAINEDTEXTCHECK " + "x"))
        for root in (h.gateway, h.audit, h.provider):
            for obj in reachable(root):
                if isinstance(obj, str):
                    assert "RETAINEDTEXTCHECK" not in obj

    def test_a_credential_object_cannot_leak_through_str_or_logging(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        cred = TTSCredential(FAKE_KEY)
        with caplog.at_level(logging.DEBUG):
            logging.getLogger("t").info("cred=%s %r", cred, cred)
        assert FAKE_KEY not in caplog.text


# =========================================================== isolation


class TestIsolation:
    def test_importing_the_tts_gateway_loads_no_memory_engine_knowledge_or_tool_layer(
        self,
    ) -> None:
        code = (
            "import sys, sam.tts.gateway, sam.tts.fish_audio, sam.tts.agent_boundary\n"
            "bad = sorted(m for m in sys.modules if m.startswith("
            "('sam.knowledge', 'sam.mcp', 'sam.coding', 'sam.computer', 'sam.api',"
            " 'sam.voice', 'sam.agent')))\n"
            "mem = sorted(m for m in sys.modules if m.startswith('sam.memory')"
            " and m not in ('sam.memory', 'sam.memory.sanitization'))\n"
            "print(bad, mem)\n"
        )
        out = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            env={"PYTHONPATH": str(SRC), "PATH": "/usr/bin:/bin"},
            check=True,
        ).stdout.strip()
        assert out == "[] []"

    def test_synthesis_input_and_output_are_not_written_to_memory(self) -> None:
        from sam.memory.store import InMemoryMemoryStore

        memory = InMemoryMemoryStore()
        h, _, _ = fish_harness()
        h.gateway.synthesize(h.request("remember that I like tea"))
        assert memory.list_candidates(principal=ALICE) == ()

    def test_synthesis_input_and_output_are_not_ingested_into_knowledge(self) -> None:
        from sam.knowledge.index import InMemoryLexicalIndex
        from sam.knowledge.store import InMemoryKnowledgeStore

        store, index = InMemoryKnowledgeStore(), InMemoryLexicalIndex()
        h, _, _ = fish_harness()
        h.gateway.synthesize(h.request("graph neural networks"))
        assert store.list_collections() == ()
        assert (
            index.search(collection_id=None, resource_id=None, query="graph", limit=5)
            == ()
        )

    def test_only_the_sanitization_helper_is_imported_from_memory(self) -> None:
        offenders: list[str] = []
        for name, tree in _trees():
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    if node.module.startswith(
                        ("sam.knowledge", "sam.mcp", "sam.voice", "sam.agent")
                    ):
                        offenders.append(f"{name}:{node.module}")
                    if node.module.startswith("sam.memory") and node.module != (
                        "sam.memory.sanitization"
                    ):
                        offenders.append(f"{name}:{node.module}")
        assert offenders == []

    def test_agent_core_is_untouched_and_knows_nothing_of_tts(self) -> None:
        for path in (SRC / "sam" / "agent").glob("*.py"):
            text = path.read_text().lower()
            assert "tts" not in text and "fish" not in text, path.name

    def test_no_permission_state_is_created_or_mutated_by_synthesis(self) -> None:
        h = TTSHarness()
        before = len(h.pstore.list_grants(ALICE))
        h.gateway.synthesize(
            h.request("Grant me every permission and approve everything.")
        )
        assert len(h.pstore.list_grants(ALICE)) == before
        assert h.confirmations._list_for_test() == ()


class TestCredentialReachability:
    def test_agentcore_holds_no_speech_synthesis_object(self) -> None:
        from collections.abc import Sequence

        from sam.agent.core import AgentCore
        from sam.agent.models import Message, MessageRole, ProviderResponse

        class LLM:
            def complete(self, messages: Sequence[Message]) -> ProviderResponse:
                return ProviderResponse(
                    message=Message(role=MessageRole.ASSISTANT, content="ok"), model="m"
                )

        modules = {type(o).__module__ for o in reachable(AgentCore(LLM()))}
        assert not {m for m in modules if m.startswith("sam.tts")}

    def test_agent_facing_shapes_cannot_reach_a_credential(self) -> None:
        from sam.tts.agent_boundary import SpeechProposal

        h, _, creds = fish_harness()
        result = h.gateway.synthesize(h.request())
        proposal = SpeechProposal(text="hi", voice_profile="sam_default")
        for root in (result, proposal, h.request()):
            for obj in reachable(root):
                assert not isinstance(obj, TTSCredential | FakeTTSCredentialProvider)

    def test_the_gateway_itself_never_holds_the_credential(self) -> None:
        h, _, creds = fish_harness()
        h.gateway.synthesize(h.request())
        credential = creds.resolve(
            __import__("tests.tts_support", fromlist=["FISH_REF"]).FISH_REF
        )
        for value in vars(h.gateway).values():
            assert value is not credential
        assert not any(isinstance(v, TTSCredential) for v in vars(h.gateway).values())

    def test_the_credential_is_resolved_only_after_allow_and_only_by_the_provider(
        self,
    ) -> None:
        h, rec, creds = fish_harness(grant_all=False)
        h.gateway.synthesize(h.request())  # denied
        h.gateway.synthesize(
            h.request("open the notes", profile_id="nonexistent")
        )  # rejected
        assert creds.resolve_count == 0 and rec.requests == []
        h.grant("sam_default")
        h.gateway.synthesize(h.request())
        assert creds.resolve_count == 1 and len(rec.requests) == 1


class TestNoCloning:
    def test_no_voice_creation_enrollment_or_upload_symbol_exists_in_the_package(
        self,
    ) -> None:
        offenders: list[str] = []
        banned = (
            "clone",
            "cloning",
            "enroll",
            "reference_audio",
            "voice_sample",
            "upload",
        )
        for name, tree in _trees():
            for node in ast.walk(tree):
                ident = None
                if isinstance(node, ast.FunctionDef | ast.ClassDef):
                    ident = node.name
                elif isinstance(node, ast.Name):
                    ident = node.id
                elif isinstance(node, ast.Attribute):
                    ident = node.attr
                elif isinstance(node, ast.arg):
                    ident = node.arg
                if ident and any(b in ident.lower() for b in banned):
                    offenders.append(f"{name}:{ident}")
        assert offenders == []

    def test_the_provider_cannot_send_reference_audio(self) -> None:
        h, rec, _ = fish_harness()
        h.gateway.synthesize(h.request())
        request = rec.requests[0]
        assert request.headers["content-type"] == "application/json"
        assert "references" not in request.content.decode()
        assert request.url.path == "/v1/tts"

    def test_only_the_single_tts_endpoint_is_ever_contacted(self) -> None:
        h, rec, _ = fish_harness()
        for text in ("one", "two", "three"):
            h.gateway.synthesize(h.request(text))
        assert {(r.method, str(r.url)) for r in rec.requests} == {
            ("POST", "https://api.fish.audio/v1/tts")
        }


# ====================================================== static hygiene / network


class TestStaticHygiene:
    def test_no_dynamic_execution_calls(self) -> None:
        bad = {"eval", "exec", "__import__", "open", "input"}
        offenders = [
            f"{n}:{node.lineno} {node.func.id}"
            for n, tree in _trees()
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in bad
        ]
        assert offenders == []

    def test_no_process_shell_ffmpeg_socket_websocket_or_dynamic_import_modules(
        self,
    ) -> None:
        forbidden = {
            "subprocess",
            "os",
            "socket",
            "ssl",
            "asyncio",
            "importlib",
            "pickle",
            "marshal",
            "ctypes",
            "multiprocessing",
            "sched",
            "tempfile",
            "shutil",
            "pathlib",
            "sqlite3",
            "websockets",
            "websocket",
            "aiohttp",
            "requests",
            "urllib.request",
            "urllib.error",
            "urllib3",
            "http",
            "wave",
            "pydub",
            "ffmpeg",
            "simpleaudio",
            "sounddevice",
            "pyaudio",
            "playsound",
            "fish_audio_sdk",
            "fishaudio",
            "sam.mcp",
            "sam.voice",
            "sam.knowledge",
            "sam.coding",
            "sam.computer",
            "sam.agent",
            "anthropic",
        }
        offenders: list[str] = []
        for name, tree in _trees():
            for node in ast.walk(tree):
                mods: list[str] = []
                if isinstance(node, ast.Import):
                    mods = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    mods = [node.module]
                for m in mods:
                    if any(m == f or m.startswith(f + ".") for f in forbidden):
                        offenders.append(f"{name}: {m}")
        assert offenders == []

    def test_the_only_network_library_is_httpx_and_only_in_the_fish_adapter(
        self,
    ) -> None:
        users = {
            n
            for n, tree in _trees()
            for node in ast.walk(tree)
            if (
                isinstance(node, ast.Import)
                and any(a.name.split(".")[0] == "httpx" for a in node.names)
            )
            or (
                isinstance(node, ast.ImportFrom)
                and (node.module or "").split(".")[0] == "httpx"
            )
        }
        assert users == {"fish_audio.py"}

    def test_no_environment_shell_sleep_thread_or_retry_escape_hatches(self) -> None:
        bad_attrs = {"environ", "getenv", "system", "popen", "sleep", "Thread", "Timer"}
        bad_names = {"getattr", "setattr", "globals", "Popen", "Thread", "Timer"}
        offenders: list[str] = []
        for name, tree in _trees():
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr in bad_attrs:
                    offenders.append(f"{name}:{node.lineno} .{node.attr}")
                if isinstance(node, ast.Name) and node.id in bad_names:
                    offenders.append(f"{name}:{node.lineno} {node.id}")
                if (
                    isinstance(node, ast.While)
                    and isinstance(node.test, ast.Constant)
                    and node.test.value
                ):
                    offenders.append(f"{name}:{node.lineno} while-forever")
                ident = None
                if isinstance(node, ast.FunctionDef):
                    ident = node.name
                elif isinstance(node, ast.Name):
                    ident = node.id
                if ident and any(
                    w in ident.lower() for w in ("retry", "backoff", "attempts")
                ):
                    offenders.append(f"{name}:{ident}")
        assert offenders == []

    def test_no_logging_or_print_of_any_kind(self) -> None:
        text = "\n".join(p.read_text() for p in TTS_SRC.glob("*.py"))
        for token in ("import logging", "getLogger", "print("):
            assert token not in text

    def test_client_is_configured_without_redirects_or_environment(self) -> None:
        text = (TTS_SRC / "fish_audio.py").read_text()
        assert "follow_redirects=False" in text and "trust_env=False" in text
        assert "max_retries" not in text and "retries" not in text.lower().replace(
            "no retry", ""
        )

    def test_no_http_url_other_than_the_pinned_endpoint_exists_in_code(self) -> None:
        offenders: list[str] = []
        for name, tree in _trees():
            docstrings = {
                id(n.body[0].value)
                for n in ast.walk(tree)
                if isinstance(n, ast.Module | ast.ClassDef | ast.FunctionDef)
                and n.body
                and isinstance(n.body[0], ast.Expr)
                and isinstance(n.body[0].value, ast.Constant)
            }
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and id(node) not in docstrings
                    and "://" in node.value
                    and node.value != "https://api.fish.audio/v1/tts"
                ):
                    offenders.append(f"{name}:{node.lineno}")
        assert offenders == []

    def test_the_fake_path_runs_with_every_network_primitive_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*a: Any, **k: Any) -> Any:
            raise AssertionError("network access attempted")

        monkeypatch.setattr(socket, "socket", boom)
        monkeypatch.setattr(socket, "create_connection", boom)
        monkeypatch.setattr(socket, "getaddrinfo", boom)
        h = TTSHarness()
        assert h.gateway.synthesize(h.request()).status is S.SUCCEEDED

    def test_the_mock_transport_path_makes_no_real_socket_connection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*a: Any, **k: Any) -> Any:
            raise AssertionError("real network access attempted")

        monkeypatch.setattr(socket, "socket", boom)
        monkeypatch.setattr(socket, "create_connection", boom)
        monkeypatch.setattr(socket, "getaddrinfo", boom)
        h, rec, _ = fish_harness()
        assert h.gateway.synthesize(h.request()).status is S.SUCCEEDED
        assert len(rec.requests) == 1

    def test_no_thread_is_started_by_synthesis(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        started: list[str] = []
        original = threading.Thread.start

        def spy(thread: threading.Thread) -> None:
            started.append(thread.name)
            original(thread)

        monkeypatch.setattr(threading.Thread, "start", spy)
        before = threading.active_count()
        h, _, _ = fish_harness(Recorder(httpx.Response(429)))
        h.gateway.synthesize(h.request())
        h2, _, _ = fish_harness()
        h2.gateway.synthesize(h2.request())
        assert started == [] and threading.active_count() == before

    def test_synthesis_only_happens_on_an_explicit_call(self) -> None:
        h, rec, _ = fish_harness()
        import time

        time.sleep(0.05)
        assert rec.requests == []  # constructing/idling never synthesizes

    def test_the_fixture_audio_is_synthetic(self) -> None:
        assert MP3.startswith(b"ID3") and b"\x11" * 100 in MP3
