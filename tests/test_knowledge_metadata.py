"""Tests for sam.knowledge.metadata — secret protection."""

from __future__ import annotations

from sam.knowledge.metadata import (
    is_restricted_content,
    is_restricted_filename,
    is_restricted_metadata,
)
from sam.knowledge.models import DocumentMetadata


class TestIsRestrictedFilename:
    def test_dotenv(self) -> None:
        assert is_restricted_filename(".env")

    def test_dotenv_suffix(self) -> None:
        assert is_restricted_filename(".env.production")

    def test_pem(self) -> None:
        assert is_restricted_filename("server.pem")

    def test_key(self) -> None:
        assert is_restricted_filename("private.key")

    def test_credentials(self) -> None:
        assert is_restricted_filename("credentials.json")

    def test_secrets(self) -> None:
        assert is_restricted_filename("secrets.yaml")

    def test_ssh_key(self) -> None:
        assert is_restricted_filename("id_rsa")
        assert is_restricted_filename("id_ed25519")

    def test_ordinary_file_not_restricted(self) -> None:
        assert not is_restricted_filename("notes.txt")

    def test_matches_only_final_segment(self) -> None:
        assert is_restricted_filename("some/path/.env")
        assert not is_restricted_filename("credentials_reader.py")


class TestIsRestrictedContent:
    def test_api_key(self) -> None:
        assert is_restricted_content("sk-ant-abcdefghijklmnopqrstuvwxyz123456")

    def test_ordinary_text_not_restricted(self) -> None:
        assert not is_restricted_content("Graph neural networks are useful.")


class TestIsRestrictedMetadata:
    def test_title_with_secret(self) -> None:
        meta = DocumentMetadata(title="sk-ant-abcdefghijklmnopqrstuvwxyz123456")
        assert is_restricted_metadata(meta)

    def test_custom_value_with_secret(self) -> None:
        meta = DocumentMetadata(custom={"note": "password is hunter2xyz"})
        assert is_restricted_metadata(meta)

    def test_ordinary_metadata_not_restricted(self) -> None:
        meta = DocumentMetadata(title="Deep Learning", author="Ian Goodfellow")
        assert not is_restricted_metadata(meta)
