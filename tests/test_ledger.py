"""Tests for scripts/ledger.py and schemas/asset_ledger_schema.json."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../scripts")))

import ledger  # noqa: E402

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schemas" / "asset_ledger_schema.json"


def make_step(
    *,
    actor: str = "human",
    tool: str = "Aseprite",
    summary: str = "Hand-authored sprite.",
    prompt: str | None = None,
    **extra,
) -> dict:
    step = {"actor": actor, "tool": tool, "summary": summary, **extra}
    if actor == "ai":
        step["prompt"] = prompt or "Prompt text"
    elif prompt is not None:
        step["prompt"] = prompt
    return step


def validate_with_jsonschema(data: dict) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.validate(instance=data, schema=schema)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "game"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "dev@crenet.games"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Dev"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)
    (repo / ".gitignore").write_text("artifacts/storefront/*\n", encoding="utf-8")
    (repo / "README.md").write_text("# Game\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    return repo


@pytest.mark.parametrize(
    ("actors", "expected_ai_involvement"),
    [
        (["human"], "none"),
        (["procedural"], "none"),
        (["human", "procedural"], "none"),
        (["ai"], "generated"),
        (["ai", "ai"], "generated"),
        (["ai", "human"], "assisted"),
        (["human", "ai", "procedural"], "assisted"),
    ],
)
def test_compute_ai_involvement(actors: list[str], expected_ai_involvement: str):
    steps = [make_step(actor=a) for a in actors]
    assert ledger.compute_ai_involvement(steps) == expected_ai_involvement


@pytest.mark.parametrize(
    "bad_steps",
    [
        [],
        [make_step(actor="human") for _ in range(7)],
        [{"actor": "ai", "tool": "Imagen", "summary": "missing prompt"}],
        [make_step(actor="human", prompt="forbidden prompt")],
        [make_step(actor="procedural", prompt="forbidden prompt")],
    ],
)
def test_invalid_steps_rejected_against_code_and_schema(tmp_path: Path, bad_steps: list[dict]):
    img = tmp_path / "sprite.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")
    with pytest.raises((ValueError, ledger.LedgerCorruptError)):
        ledger.log_asset(tmp_path, "sprite.png", bad_steps)


def test_log_asset_auto_hashes_and_matches_draft07_schema(tmp_path: Path):
    ref = tmp_path / "artifacts/sketch.png"
    ref.parent.mkdir(parents=True)
    ref.write_bytes(b"sketch-bytes")
    out = tmp_path / "client/public/hero.png"
    out.parent.mkdir(parents=True)
    out.write_bytes(b"final-hero-bytes")

    entry = ledger.log_asset(
        tmp_path,
        "client/public/hero.png",
        [
            make_step(actor="human", tool="Krita", summary="Rough silhouette sketch."),
            make_step(
                actor="ai",
                tool="gemini-imagen",
                prompt="Clean sci-fi armor silhouette",
                artifact_ref="artifacts/sketch.png",
                seed=42,
                license="Commercial-Studio",
            ),
            make_step(actor="human", tool="Aseprite", summary="Pixel paint-over."),
        ],
    )
    assert entry["ai_involvement"] == "assisted"
    assert entry["sha256"] == ledger.compute_sha256(out)
    assert entry["steps"][1]["sha256"] == ledger.compute_sha256(ref)

    raw_ledger = json.loads((tmp_path / "docs/assets_ledger.json").read_text(encoding="utf-8"))
    validate_with_jsonschema(raw_ledger)


def test_discovers_gitignored_artifacts_respects_ignore_and_ignores_unstaged_deletes(git_repo: Path):
    draft = git_repo / "artifacts/storefront/raw_banner.png"
    draft.parent.mkdir(parents=True)
    draft.write_bytes(b"raw")
    pub = git_repo / "client/public/logo.png"
    pub.parent.mkdir(parents=True)
    pub.write_bytes(b"logo")
    ignored = git_repo / "client/public/debug_temp.png"
    ignored.write_bytes(b"debug")
    deleted = git_repo / "client/public/old.png"
    deleted.write_bytes(b"old")
    subprocess.run(["git", "add", "client/public/old.png"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-m", "add old"], cwd=git_repo, check=True, capture_output=True)
    deleted.unlink()  # Unstaged deletion in working tree

    (git_repo / ".assetledgerignore").write_text("debug_*.png\n", encoding="utf-8")

    discovered = ledger.discover_repo_assets(git_repo)
    assert "artifacts/storefront/raw_banner.png" in discovered
    assert "client/public/logo.png" in discovered
    assert "client/public/debug_temp.png" not in discovered
    assert "client/public/old.png" not in discovered

    verify = ledger.verify_ledger(git_repo)
    assert verify["ok"] is False
    assert set(verify["unlogged"]) == {"artifacts/storefront/raw_banner.png", "client/public/logo.png"}


def test_graduation_move_and_sync_moves(git_repo: Path):
    src = git_repo / "artifacts/storefront/concept.png"
    src.parent.mkdir(parents=True)
    content = b"identical-image-payload"
    src.write_bytes(content)

    ledger.log_asset(
        git_repo,
        "artifacts/storefront/concept.png",
        [make_step(actor="ai", tool="imagen-3", prompt="Modular corridor key art")],
    )

    dst = git_repo / "client/public/key-art.png"
    dst.parent.mkdir(parents=True)
    src.rename(dst)

    # Verify detects move candidate before sync-moves
    pre_verify = ledger.verify_ledger(git_repo)
    assert pre_verify["ok"] is False
    assert len(pre_verify["graduation_candidates"]) == 1
    assert pre_verify["graduation_candidates"][0]["orphan_match"] is True

    sync_res = ledger.sync_moves(git_repo)
    assert len(sync_res["moved"]) == 1
    assert sync_res["moved"][0]["from"] == "artifacts/storefront/concept.png"

    post_verify = ledger.verify_ledger(git_repo)
    assert post_verify["ok"] is True

    # Explicit graduation with human step updates ai_involvement -> assisted
    dst2 = git_repo / "client/public/banner.png"
    dst.rename(dst2)
    moved_entry = ledger.move_asset(
        git_repo,
        "client/public/key-art.png",
        "client/public/banner.png",
        step=make_step(actor="human", tool="GIMP", summary="Cropped and branded."),
    )
    assert moved_entry["ai_involvement"] == "assisted"
    assert moved_entry["previous_paths"] == [
        "artifacts/storefront/concept.png",
        "client/public/key-art.png",
    ]


def test_corrupt_ledger_and_config_fail_loud_and_opt_out(git_repo: Path):
    (git_repo / "docs").mkdir(exist_ok=True)
    (git_repo / "docs/assets_ledger.json").write_text('{"assets": {}}', encoding="utf-8")
    with pytest.raises(ledger.LedgerCorruptError):
        ledger.verify_ledger(git_repo)

    (git_repo / "docs/assets_ledger.json").write_text("{bad json", encoding="utf-8")
    with pytest.raises(ledger.LedgerCorruptError):
        ledger.verify_ledger(git_repo)

    # Opt-out in .crenet-governance.json bypasses ledger enforcement cleanly
    (git_repo / "client.png").write_bytes(b"img")
    (git_repo / ".crenet-governance.json").write_text('{"asset_provenance": false}\n', encoding="utf-8")
    res = ledger.verify_ledger(git_repo)
    assert res["enabled"] is False
    assert res["ok"] is True


@pytest.mark.parametrize(
    ("v1_payload", "expected_involvement", "expected_status"),
    [
        pytest.param(
            {
                "assets": {
                    "hero.png": {
                        "asset_path": "client/public/hero.png",
                        "generation_tool": "gemini-imagen + Aseprite",
                        "prompt": "Cyberpunk visor hero",
                        "provenance_tier": "hybrid",
                        "status": "active",
                        "logged_at": "2026-01-01T00:00:00Z",
                    }
                }
            },
            "assisted",
            "active",
            id="hybrid_ai_plus_human",
        ),
        pytest.param(
            {
                "assets": {
                    "icon.svg": {
                        "asset_path": "client/public/icon.svg",
                        "generation_tool": "Manual SVG vector art",
                        "prompt": "N/A - manual vector icon",
                        "status": "deprecated_deleted",
                        "logged_at": "2026-01-01T00:00:00Z",
                    }
                }
            },
            "none",
            "deprecated",
            id="na_prompt_procedural_deprecated",
        ),
        pytest.param(
            {
                "assets": {
                    "keyart.png": {
                        "generation_tool": "Imagen 3",
                        "prompt": "Orbital elevator in dusk lighting",
                        "status": "active",
                    }
                }
            },
            "generated",
            "active",
            id="pure_ai_entry",
        ),
        pytest.param(
            [
                {
                    "file_name": "/tmp/some-abs-path/tile.png",
                    "asset_path": "client/public/assets/tile.png",
                    "generation_type": "pure_ai",
                    "timestamp_utc": "2026-10-26T20:39:26+00:00",
                    "ai_generation_data": {
                        "prompt": "Tile prompt",
                        "model_used": "Procedural Python Asset Generator (gen.py)",
                    },
                }
            ],
            "none",
            "active",
            id="root_list_procedural_py_entry",
        ),
        pytest.param(
            [
                {
                    "asset_path": "shared/public/assets/flat-capital-hex.png",
                    "generation_type": "ai_to_post_edit",
                    "ai_generation_data": {
                        "prompt": "Hand-painted fantasy capital hex tile",
                        "model_used": "Gemini API Image Gen",
                    },
                    "human_post_edit_data": {
                        "modification_description": "Resized and removed background fringe."
                    },
                }
            ],
            "assisted",
            "active",
            id="ai_to_post_edit_tier_entry",
        ),
    ],
)
def test_migrate_legacy_v1_ledger_validates_against_schema(
    tmp_path: Path,
    v1_payload: dict,
    expected_involvement: str,
    expected_status: str,
):
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "assets_ledger.json").write_text(json.dumps(v1_payload), encoding="utf-8")

    mig = ledger.migrate_legacy_ledger(tmp_path)
    assert mig["already_v2"] is False
    assert mig["migrated_count"] == 1

    v2_data = json.loads((docs_dir / "assets_ledger.json").read_text(encoding="utf-8"))
    validate_with_jsonschema(v2_data)
    entry = next(iter(v2_data["assets"].values()))
    assert entry["ai_involvement"] == expected_involvement
    assert entry["status"] == expected_status


def test_generate_report_scorecard(tmp_path: Path):
    (tmp_path / "a.png").write_bytes(b"a")
    (tmp_path / "b.png").write_bytes(b"b")
    ledger.log_asset(tmp_path, "a.png", [make_step(actor="human", tool="Krita", summary="Art.")])
    ledger.log_asset(tmp_path, "b.png", [make_step(actor="ai", tool="Veo", prompt="Sci-fi corridor")])

    report = ledger.generate_report(tmp_path)
    assert report["ok"] is True
    assert report["counts"]["ai_involvement"] == {"none": 1, "assisted": 0, "generated": 1}
    md = ledger.format_report_markdown(report)
    assert "Asset Provenance Scorecard" in md
    assert "`PASS`" in md
