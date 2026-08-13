"""The artifact model: contracts, content addressing, the store.

LF-5 D8 and the architecture note's section 5. Nothing here goes through the
CLI: an artifact is a value plus a file, and both are worth pinning on their
own before any command depends on them.

The identifier is the load-bearing part. Lineage is a graph of ids, so an
unstable id silently detaches a rejection from the artifact it rejected (R6).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from lazyfish.artifacts import (
    CONTRACTS,
    ROLE_ARCHITECT,
    TYPE_TECHNICAL_PLAN,
    Artifact,
    ArtifactStore,
    canonical_bytes,
    content_id,
)
from lazyfish.errors import ValidationError, WorkspaceError
from lazyfish.schema import (
    RULE_FILES_EXIST,
    RULE_NEEDS_HUMAN,
    RULE_REQUIRED_ARRAYS,
    RULE_SCHEMA,
)

from .conftest import make_plan

VALIDATOR_RULES = frozenset({RULE_SCHEMA, RULE_REQUIRED_ARRAYS, RULE_NEEDS_HUMAN, RULE_FILES_EXIST})


def an_artifact(**overrides: object) -> Artifact:
    fields: dict[str, object] = {
        "id": content_id(make_plan()),
        "type": TYPE_TECHNICAL_PLAN,
        "produced_by": ROLE_ARCHITECT,
        "task_id": 1,
        "base_commit": "0" * 40,
        "parents": (),
        "promoted_at": None,
        "attempt": 0,
    }
    fields.update(overrides)
    return Artifact(**fields)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# AC10: the identifier is the content
# --------------------------------------------------------------------------- #


def test_the_same_plan_extracted_twice_gets_the_same_id() -> None:
    """AC10, first half."""
    assert content_id(make_plan()) == content_id(make_plan())


def test_key_order_does_not_change_the_id() -> None:
    """A dict is unordered as a value; a hash over one must agree (R6)."""
    plan = make_plan()
    reordered = dict(reversed(list(plan.items())))
    assert list(reordered) != list(plan)
    assert content_id(reordered) == content_id(plan)


def test_nested_key_order_does_not_change_the_id() -> None:
    """Ordering has to be canonicalised all the way down, not only at the top."""
    plan = make_plan()
    nested = json.loads(json.dumps(plan))
    nested["changes"][0] = dict(reversed(list(nested["changes"][0].items())))
    assert content_id(nested) == content_id(plan)


def test_list_order_does_change_the_id() -> None:
    """Sorting keys is canonicalisation; sorting lists would be data loss.

    The order of `changes` is part of what the Architect said.
    """
    plan = make_plan(
        changes=[
            {"file": "a.py", "action": "modify", "reason": "first"},
            {"file": "b.py", "action": "modify", "reason": "second"},
        ]
    )
    swapped = make_plan(changes=list(reversed(plan["changes"])))  # type: ignore[arg-type]
    assert content_id(swapped) != content_id(plan)


def test_one_changed_character_changes_the_id() -> None:
    """AC10, second half."""
    plan = make_plan()
    edited = make_plan(understanding=str(plan["understanding"]) + ".")
    assert content_id(edited) != content_id(plan)


def test_the_id_is_a_hexadecimal_digest() -> None:
    """Content addressed, not an autoincrement (D8)."""
    identifier = content_id(make_plan())
    assert identifier
    assert identifier == identifier.lower()
    assert all(character in "0123456789abcdef" for character in identifier)
    assert len(identifier) >= 32


def test_the_serialisation_is_stable_across_processes() -> None:
    """R6: an id that depends on interpreter state breaks lineage between runs."""
    script = (
        "import json, sys;"
        "from lazyfish.artifacts import content_id;"
        "print(content_id(json.loads(sys.stdin.read())))"
    )
    plan = make_plan(understanding="stability check")
    digests = set()
    for seed in ("0", "1", "12345"):
        environment = dict(os.environ, PYTHONHASHSEED=seed)
        result = subprocess.run(
            [sys.executable, "-c", script],
            input=json.dumps(plan),
            capture_output=True,
            text=True,
            env=environment,
            check=True,
        )
        digests.add(result.stdout.strip())
    assert digests == {content_id(plan)}


def test_the_serialisation_keeps_non_ascii_content_as_written() -> None:
    """The passthrough rule reaches the artifact store too (LF-1 AC15)."""
    plan = make_plan(understanding="重置令牌的有效期被设置为一小时。")
    serialised = canonical_bytes(plan)
    assert "重置令牌的有效期被设置为一小时。".encode() in serialised
    assert b"\\u" not in serialised


def test_the_serialisation_is_byte_identical_for_equal_content() -> None:
    plan = make_plan()
    assert canonical_bytes(plan) == canonical_bytes(json.loads(json.dumps(plan)))


# --------------------------------------------------------------------------- #
# The contract registry
# --------------------------------------------------------------------------- #


def test_only_the_technical_plan_contract_is_registered() -> None:
    """One stage this slice: four unverified contracts at once is A-3."""
    assert set(CONTRACTS) == {TYPE_TECHNICAL_PLAN}


def test_the_contract_names_its_producer() -> None:
    assert CONTRACTS[TYPE_TECHNICAL_PLAN].produced_by == ROLE_ARCHITECT


def test_the_contract_extracts_only_the_plan_from_a_workspace(tmp_path: Path) -> None:
    """Extraction is by contract, not by looking at what changed (D1)."""
    workspace = tmp_path / "workspace"
    (workspace / ".lazyfish").mkdir(parents=True)
    (workspace / ".lazyfish" / "plan.json").write_text(json.dumps(make_plan()), encoding="utf-8")
    (workspace / "stray.py").write_text("MARKER = 1\n", encoding="utf-8")

    extracted = CONTRACTS[TYPE_TECHNICAL_PLAN].extract(workspace)
    assert extracted == make_plan()
    assert "stray.py" not in json.dumps(extracted)


def test_extraction_without_a_plan_says_where_to_put_it(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(ValidationError, match="plan.json"):
        CONTRACTS[TYPE_TECHNICAL_PLAN].extract(workspace)


def test_the_contract_validates_with_the_existing_rules(tmp_path: Path) -> None:
    """A2: the plan contract already exists; this slice reuses it, not replaces it."""
    workspace = tmp_path / "workspace"
    (workspace / "src" / "auth").mkdir(parents=True)
    (workspace / "src" / "auth" / "reset_token.py").write_text("", encoding="utf-8")

    contract = CONTRACTS[TYPE_TECHNICAL_PLAN]
    assert contract.validate(make_plan(), workspace) == []

    issues = contract.validate(make_plan(needs_human=False), workspace)
    assert [issue.rule for issue in issues] == [RULE_NEEDS_HUMAN]
    assert all(issue.rule in VALIDATOR_RULES for issue in issues)


def test_the_contract_checks_declared_paths_against_the_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    issues = CONTRACTS[TYPE_TECHNICAL_PLAN].validate(make_plan(), workspace)
    assert [issue.rule for issue in issues] == [RULE_FILES_EXIST]


# --------------------------------------------------------------------------- #
# The store
# --------------------------------------------------------------------------- #


def test_content_round_trips_through_the_store(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    plan = make_plan(understanding="重置令牌")
    artifact = an_artifact(id=content_id(plan))

    store.store(artifact, plan)
    assert store.load(artifact.task_id, artifact.id) == plan


def test_the_store_lays_content_out_by_task_and_id(tmp_path: Path) -> None:
    """D8: the layout is part of the design, not an implementation detail."""
    root = tmp_path / "artifacts"
    store = ArtifactStore(root)
    plan = make_plan()
    artifact = an_artifact(id=content_id(plan), task_id=7)

    written = store.store(artifact, plan)
    assert written == store.path_for(7, artifact.id)
    assert written == root / "7" / artifact.id
    assert written.is_file()


def test_storing_the_same_content_twice_is_idempotent(tmp_path: Path) -> None:
    """AC10, third part: promoting the same plan again changes nothing."""
    root = tmp_path / "artifacts"
    store = ArtifactStore(root)
    plan = make_plan()
    artifact = an_artifact(id=content_id(plan))

    first = store.store(artifact, plan)
    before = first.read_bytes()
    second = store.store(artifact, plan)

    assert second == first
    assert first.read_bytes() == before
    assert sorted(path.name for path in (root / "1").iterdir()) == [artifact.id]


def test_a_second_attempt_is_stored_beside_the_first(tmp_path: Path) -> None:
    """Retries produce several artifacts for one stage; none overwrites another."""
    root = tmp_path / "artifacts"
    store = ArtifactStore(root)
    first_plan = make_plan()
    second_plan = make_plan(understanding="a second attempt at the same ticket")

    store.store(an_artifact(id=content_id(first_plan)), first_plan)
    store.store(an_artifact(id=content_id(second_plan), attempt=1), second_plan)

    assert len(list((root / "1").iterdir())) == 2


def test_loading_an_unknown_artifact_is_an_error(tmp_path: Path) -> None:
    """A store failure is a workspace-level failure; the errata assigns no new type."""
    store = ArtifactStore(tmp_path / "artifacts")
    with pytest.raises(WorkspaceError):
        store.load(1, "0" * 64)


def test_the_store_never_writes_into_the_target_repository(tmp_path: Path, repo: Path) -> None:
    """A-5 and D8: the workspace is the agent's, not the tool's storage."""
    before = sorted(path.relative_to(repo) for path in repo.rglob("*") if ".git" not in path.parts)
    store = ArtifactStore(tmp_path / "artifacts")
    plan = make_plan()
    store.store(an_artifact(id=content_id(plan)), plan)
    after = sorted(path.relative_to(repo) for path in repo.rglob("*") if ".git" not in path.parts)
    assert after == before


# --------------------------------------------------------------------------- #
# Lineage
# --------------------------------------------------------------------------- #


def test_an_artifact_carries_the_lineage_fields() -> None:
    """The architecture note's section 5, field for field."""
    artifact = an_artifact()
    for name in (
        "id",
        "type",
        "produced_by",
        "task_id",
        "base_commit",
        "parents",
        "promoted_at",
        "attempt",
    ):
        assert hasattr(artifact, name), name


def test_an_unpromoted_artifact_has_no_promotion_time() -> None:
    """promoted_at is null until the Orchestrator says otherwise (section 5)."""
    assert an_artifact().promoted_at is None


def test_the_architect_consumes_no_artifacts() -> None:
    """The authority table: Architect consumes the Ticket, nothing promoted."""
    assert an_artifact().parents == ()
