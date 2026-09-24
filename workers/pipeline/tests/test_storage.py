"""Storage layer: key layout (2.4) and the local backend.

The S3 backend is not exercised here. Mocking botocore would test the mock,
and the real thing is checked against R2 by the infrastructure verification in
`docs/infrastructure-setup.md` once credentials exist. What is worth testing
without a network is the part that is ours: the key format every artefact and
every lifecycle rule depends on, and the fallback that decides which backend
a run gets.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from roomfittr_pipeline.errors import PipelineError
from roomfittr_pipeline.storage import (
    LocalObjectStore,
    S3ObjectStore,
    artefact_key,
    from_env,
)

R2_VARS = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET", "R2_ENDPOINT")


def test_key_matches_the_layout_2_4_fixes() -> None:
    assert (
        artefact_key(
            room_id="r1", scan_id="s1", pipeline_version="0.1.0", stage="fuse", filename="a.ply"
        )
        == "rooms/r1/scans/s1/0.1.0/fuse/a.ply"
    )


def test_unknown_stage_is_rejected() -> None:
    # `errors.Stage` is the vocabulary; the runbook's lifecycle rules expire
    # those prefixes. A stage name that is merely plausible would never be
    # expired, and the symptom is a storage bill rather than an error.
    with pytest.raises(ValueError, match="unknown stage"):
        artefact_key(
            room_id="r1", scan_id="s1", pipeline_version="0.1.0", stage="fuze", filename="a.ply"
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("room_id", ""),
        ("room_id", "a/b"),
        ("scan_id", ".."),
        ("pipeline_version", ""),
    ],
)
def test_ids_that_would_corrupt_the_key_are_rejected(field: str, value: str) -> None:
    kwargs = {
        "room_id": "r1",
        "scan_id": "s1",
        "pipeline_version": "0.1.0",
        "stage": "geometry",
        "filename": "room.json",
        field: value,
    }
    with pytest.raises(ValueError):
        artefact_key(**kwargs)


def test_round_trip_bytes_and_files(tmp_path: Path) -> None:
    store = LocalObjectStore(root=tmp_path / "store")
    source = tmp_path / "in.bin"
    source.write_bytes(b"\x00\x01\x02")

    store.put_bytes("rooms/r/scans/s/0.1.0/geometry/room.json", b"{}")
    store.put_file("rooms/r/scans/s/0.1.0/fuse/a.ply", source)

    assert store.get_bytes("rooms/r/scans/s/0.1.0/geometry/room.json") == b"{}"
    out = store.get_file("rooms/r/scans/s/0.1.0/fuse/a.ply", tmp_path / "out.bin")
    assert out.read_bytes() == b"\x00\x01\x02"
    assert store.exists("rooms/r/scans/s/0.1.0/geometry/room.json")
    assert not store.exists("rooms/r/scans/s/0.1.0/geometry/nope.json")


def test_missing_artefact_raises_a_pipeline_error(tmp_path: Path) -> None:
    # Not FileNotFoundError: 7.4 wants stages to fail in the taxonomy, so the
    # job's failure reason survives to the operator without translation.
    store = LocalObjectStore(root=tmp_path)
    with pytest.raises(PipelineError):
        store.get_bytes("rooms/r/scans/s/0.1.0/geometry/absent.json")


def test_list_is_prefix_scoped(tmp_path: Path) -> None:
    store = LocalObjectStore(root=tmp_path)
    store.put_bytes("rooms/r/scans/s/0.1.0/frames/0001.jpg", b"a")
    store.put_bytes("rooms/r/scans/s/0.1.0/frames/0002.jpg", b"b")
    store.put_bytes("rooms/r/scans/s/0.1.0/geometry/room.json", b"{}")

    frames = list(store.list("rooms/r/scans/s/0.1.0/frames/"))
    assert frames == [
        "rooms/r/scans/s/0.1.0/frames/0001.jpg",
        "rooms/r/scans/s/0.1.0/frames/0002.jpg",
    ]


def test_traversal_outside_the_root_is_refused(tmp_path: Path) -> None:
    store = LocalObjectStore(root=tmp_path / "store")
    with pytest.raises(ValueError, match="escapes"):
        store.put_bytes("../../escaped.txt", b"x")


def test_from_env_falls_back_to_local_without_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for var in R2_VARS:
        monkeypatch.delenv(var, raising=False)
    store = from_env(local_root=tmp_path)
    assert isinstance(store, LocalObjectStore)


def test_from_env_treats_the_placeholder_endpoint_as_unconfigured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # This is the state `.env.example` ships in. Half-filled credentials
    # should fall back rather than fail with a DNS error three stages into a
    # run, when the artefact that failed to write is the expensive one.
    monkeypatch.setenv("R2_ACCOUNT_ID", "acct")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "key")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("R2_BUCKET", "roomfittr-dev")
    monkeypatch.setenv("R2_ENDPOINT", "https://REPLACE.r2.cloudflarestorage.com")

    assert isinstance(from_env(local_root=tmp_path), LocalObjectStore)


def test_from_env_selects_r2_when_fully_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("R2_ACCOUNT_ID", "acct")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "key")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("R2_BUCKET", "roomfittr-dev")
    monkeypatch.setenv("R2_ENDPOINT", "https://acct.r2.cloudflarestorage.com")

    store = from_env()
    assert isinstance(store, S3ObjectStore)
    assert store.bucket == "roomfittr-dev"
    # Constructed without touching the network: no client is built until a
    # call needs one, which is what lets CI import this module at all.
    assert store._client is None
