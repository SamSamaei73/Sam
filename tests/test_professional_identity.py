"""The owner's professional identity: exact, trusted, and never guessed.

The owner is matched in an author list ONLY by the configured canonical name or
an explicitly approved alias, after deterministic normalization: no surname-only
match, no guessed initials, no fuzzy or embedding similarity, no model. No match
or an ambiguous match is UNKNOWN. Model output cannot add an alias and Guest
cannot change the configuration (there is no route for it at all).

Synthetic names and papers only.
"""

from __future__ import annotations

import dataclasses
import inspect

import pytest

from sam.core.config import Settings
from sam.desktop.runtime import _owner_identity
from sam.professional import identity as identity_module
from sam.professional.candidates import validate_candidates
from sam.professional.identity import (
    AuthorPosition,
    OwnerMatch,
    OwnerProfessionalIdentity,
    normalize_person_name,
)
from sam.professional.models import SourceLocation, SourceType
from sam.professional.reader import SourceSegment
from sam.professional.service import ProfessionalService
from sam.professional.tool import READ_OPERATIONS
from tests.professional_support import OWNER, PAPER_TEXT, Rig, make_rig

IDENTITY = OwnerProfessionalIdentity("Jordan Example")
WITH_ALIAS = OwnerProfessionalIdentity("Jordan Example", ("J. Example",))


def _position(identity: OwnerProfessionalIdentity, *authors: str) -> AuthorPosition:
    return identity.author_position(list(authors))


# ---------------------------------------------------------------- matching


def test_the_exact_canonical_name_matches() -> None:
    found = _position(IDENTITY, "Alex Sample", "Jordan Example", "Riley Placeholder")
    assert found == AuthorPosition(OwnerMatch.MATCHED, 2)


@pytest.mark.parametrize(
    "variant",
    [
        "JORDAN EXAMPLE",
        "jordan  example",
        "Jordan-Example",
        "Jördan Exámple",
        "Jordan Example*",
    ],
)
def test_normalization_is_deterministic_and_only_cosmetic(variant: str) -> None:
    cleaned = variant.rstrip("*")
    assert _position(IDENTITY, cleaned).status is OwnerMatch.MATCHED
    assert normalize_person_name(cleaned) == "jordan example"


def test_an_approved_alias_matches() -> None:
    assert _position(WITH_ALIAS, "A. Sample", "J. Example") == AuthorPosition(
        OwnerMatch.MATCHED, 2
    )


@pytest.mark.parametrize(
    "author",
    [
        "Example",  # surname only
        "J. Example",  # an initial nobody approved
        "J Example",
        "Jordan E.",
        "Jordan",  # given name only
        "Jordan Examples",  # a different (longer) name
        "Jordan Example Smith",
        "Morgan Example",  # same surname, another person
    ],
)
def test_no_surname_initial_or_partial_match(author: str) -> None:
    assert _position(IDENTITY, "Alex Sample", author).status is OwnerMatch.UNKNOWN


def test_no_match_is_unknown() -> None:
    assert _position(IDENTITY, "Alex Sample", "Riley Placeholder") == AuthorPosition(
        OwnerMatch.UNKNOWN
    )


def test_an_ambiguous_author_list_is_unknown() -> None:
    # Two authors both match (the canonical name and an approved alias).
    both = _position(WITH_ALIAS, "Jordan Example", "Alex Sample", "J. Example")
    assert both == AuthorPosition(OwnerMatch.UNKNOWN)
    twice = _position(IDENTITY, "Jordan Example", "Jordan Example")
    assert twice.status is OwnerMatch.UNKNOWN


def test_mentions_are_whole_names_never_substrings() -> None:
    assert IDENTITY.mentioned_in("Jordan Example implemented the model.")
    assert not IDENTITY.mentioned_in("Jordan Examples implemented the model.")
    assert not IDENTITY.mentioned_in("Example implemented the model.")
    assert not IDENTITY.mentioned_in("J. Example implemented the model.")


# ----------------------------------------------------------- configuration


@pytest.mark.parametrize(
    ("name", "aliases"),
    [
        ("Example", ()),  # a lone surname can never be configured
        ("Jordan Example", ("Example",)),
        ("Jordan Example", ("J.",)),
        ("Jordan Example", ("",)),
        ("Jordan Example", tuple(f"Alias Number{i}" for i in range(11))),
    ],
)
def test_invalid_identity_configuration_is_refused(
    name: str, aliases: tuple[str, ...]
) -> None:
    with pytest.raises(ValueError):
        OwnerProfessionalIdentity(name, aliases)


def test_identity_comes_from_trusted_settings_and_fails_closed() -> None:
    good = _owner_identity(
        Settings(
            owner_name="Jordan Example", owner_name_aliases="J. Example; J Q Example"
        )
    )
    assert good == OwnerProfessionalIdentity(
        "Jordan Example", ("J. Example", "J Q Example")
    )
    assert _owner_identity(Settings(owner_name=None)) is None
    # A surname-only alias invalidates the WHOLE identity: nothing is matched,
    # rather than a loosened match.
    bad = Settings(owner_name="Jordan Example", owner_name_aliases="Example")
    assert _owner_identity(bad) is None


def test_the_identity_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        IDENTITY.approved_aliases = ("Example",)  # type: ignore[misc]


def test_matching_uses_no_model_and_no_similarity() -> None:
    source = inspect.getsource(identity_module)
    imports = [
        line for line in source.splitlines() if line.startswith(("import", "from"))
    ]
    assert all("sam." not in line for line in imports)
    for forbidden in ("difflib", "levenshtein", "jaro", "embedding(", "router"):
        assert forbidden not in source.lower()


# ------------------------------------------------ through ingestion/service


def _paper_rig(identity: OwnerProfessionalIdentity | None, authors: str) -> Rig:
    rig = make_rig(owner_identity=identity)
    paper = PAPER_TEXT.replace(
        "Jordan Example, Alex Sample and Riley Placeholder", authors
    )
    assert rig.ingest(paper, name="paper.txt", source_type=SourceType.PUBLICATION).ok
    return rig


def _owner_position(rig: Rig) -> int | None:
    pubs = rig.service.get_publications(OWNER).data
    assert pubs
    return pubs[0].owner_author_position


def test_an_ingested_paper_records_the_owners_exact_position() -> None:
    rig = _paper_rig(IDENTITY, "Alex Sample, Jordan Example and Riley Placeholder")
    assert _owner_position(rig) == 2


def test_an_ingested_paper_never_guesses_from_a_surname_or_initial() -> None:
    for authors in (
        "Alex Sample, J. Example and Riley Placeholder",
        "Alex Sample, Example and Riley Placeholder",
        "Alex Sample, Morgan Example and Riley Placeholder",
    ):
        assert _owner_position(_paper_rig(IDENTITY, authors)) is None


def test_an_approved_alias_is_honoured_during_ingestion() -> None:
    rig = _paper_rig(WITH_ALIAS, "Alex Sample, J. Example and Riley Placeholder")
    assert _owner_position(rig) == 2


def test_an_ambiguous_paper_records_no_position() -> None:
    rig = _paper_rig(WITH_ALIAS, "Jordan Example, Alex Sample and J. Example")
    assert _owner_position(rig) is None


def test_without_an_identity_no_position_is_recorded() -> None:
    rig = _paper_rig(None, "Jordan Example, Alex Sample and Riley Placeholder")
    assert _owner_position(rig) is None


@pytest.mark.parametrize(
    ("writer", "attributed"),
    [
        ("Jordan Example", True),
        ("J. Example", False),
        ("Example", False),
        ("Jordan Examples", False),
    ],
)
def test_a_contribution_is_only_attributed_on_an_exact_name(
    writer: str, attributed: bool
) -> None:
    text = PAPER_TEXT.replace(
        "Riley Placeholder implemented the model.",
        f"{writer} implemented the model.",
    )
    rig = make_rig(owner_identity=IDENTITY)
    assert rig.ingest(text, name="paper.txt", source_type=SourceType.PUBLICATION).ok
    pubs = rig.service.get_publications(OWNER).data
    assert pubs
    assert (pubs[0].owner_contribution is not None) is attributed


# -------------------------------------------- model and Guest restrictions


def test_model_output_cannot_add_an_alias_or_a_position() -> None:
    hostile = {
        "candidates": [
            {
                "category": "publication",
                "statement": "Paper by the owner",
                "quote": "Semantic Embeddings for Health Misinformation Detection",
                "owner_name": "Example",
                "aliases": ["Example", "J. Example"],
                "owner_author_position": "1",
            }
        ]
    }
    segments = [SourceSegment(index=0, text=PAPER_TEXT, location=SourceLocation())]
    outcome = validate_candidates(hostile, segments)
    assert outcome.ignored_fields == 3
    assert all(c.attributes == {} for c in outcome.accepted)
    # The identity is unchanged: still exactly one canonical name, no aliases.
    assert IDENTITY.approved_aliases == ()


def test_nothing_can_change_the_identity_after_startup() -> None:
    names = [
        n for n, _ in inspect.getmembers(ProfessionalService) if not n.startswith("__")
    ]
    assert not [
        n for n in names if "identity" in n or "alias" in n or "owner_name" in n
    ]
    assert not [op for op in READ_OPERATIONS if "identity" in op or "alias" in op]


def test_there_is_no_route_that_edits_the_identity() -> None:
    from sam.desktop.professional_api import router

    paths = [getattr(r, "path", "") for r in router.routes]
    assert paths and not [p for p in paths if "identity" in p or "alias" in p]
    from sam.desktop.professional_models import (
        ProfessionalIngestRequest,
        ProfessionalReviewRequest,
    )

    for model in (ProfessionalIngestRequest, ProfessionalReviewRequest):
        assert not {"owner_name", "aliases", "identity"} & set(model.model_fields)
