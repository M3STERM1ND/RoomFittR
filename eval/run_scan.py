"""Run one capture through S1-S9 and write the artefacts (3, 2.4).

The bake-off script 2.4's repo layout asks `eval/` to hold. S1, S2 and S5-S9
run here; S3 and S4 run on a Modal GPU through `roomfittr_modal.remote`.
Artefacts go wherever `storage.from_env` points, which is R2 when its
credentials are present and a local directory otherwise -- the same code path
either way, so a local run is evidence about the pipeline rather than about
which storage it happened to use.

The prediction is written to `eval/runs/<run>/<capture_id>.json` in exactly
the shape `eval.harness` reads, so scoring a run is a separate step that
cannot accidentally depend on how the run was produced.

    uv run python -m eval.run_scan --capture gt-012-a --run vggt-l40s
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parent.parent
EVAL = REPO / "eval"
GROUND_TRUTH = EVAL / "ground_truth"


def _capture_index() -> dict[str, dict[str, Any]]:
    """capture_id -> {room_id, file}, from the committed ground truth."""
    index: dict[str, dict[str, Any]] = {}
    for path in sorted(GROUND_TRUTH.glob("gt-*.yaml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        for capture in doc.get("captures", []):
            index[capture["capture_id"]] = {
                "room_id": doc["room_id"],
                "file": EVAL / capture["file"],
                "scene_id": (doc.get("source") or {}).get("scene_id"),
            }
    return index


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", required=True, help="capture_id, e.g. gt-012-a")
    parser.add_argument("--run", required=True, help="run name under eval/runs/")
    parser.add_argument("--frames", type=int, default=64, help="keyframe budget for S3")
    parser.add_argument(
        "--segment-frames",
        type=int,
        default=16,
        help=(
            "frames sent to S4. Fewer than S3 on purpose: SAM 3 runs every prompt "
            "against every frame, so cost is frames x prompts, and S5 only needs "
            "enough views to partition structure from objects."
        ),
    )
    parser.add_argument("--no-segment", action="store_true", help="skip S4 (needs a gravity hint)")
    parser.add_argument("--gpu", default="reconstruct_vggt_l40s", help="Modal S3 function")
    args = parser.parse_args(argv)

    sys.path.insert(0, str(REPO / "workers" / "pipeline"))
    from roomfittr_pipeline import PIPELINE_VERSION, frames, ingest, pipeline, shell, storage
    from roomfittr_pipeline.errors import Stage

    captures = _capture_index()
    if args.capture not in captures:
        print(f"unknown capture {args.capture!r}; known: {sorted(captures)}")
        return 1
    entry = captures[args.capture]
    video: Path = entry["file"]
    if not video.exists():
        print(f"capture file missing: {video}")
        return 1

    work = EVAL / "runs" / args.run / "_work" / args.capture
    work.mkdir(parents=True, exist_ok=True)
    store = storage.from_env()
    print(f"capture   : {args.capture}  ({video.name}, scene {entry['scene_id']})")
    print(f"store     : {type(store).__name__}")

    timings: dict[str, float] = {}

    # S1-S2 here: decoding an 11 GB dataset over a network to a GPU container
    # would dominate the cost of a stage that is cheap on a laptop.
    t0 = time.perf_counter()
    meta = ingest.validate(ingest.probe(video))
    frames.run(video, meta, work, target=args.frames)
    timings["s1_s2"] = time.perf_counter() - t0
    model_frames = sorted((work / "keyframes" / "model").glob("*.jpg"))
    print(f"S1-S2     : {len(model_frames)} keyframes in {timings['s1_s2']:.1f}s")

    from roomfittr_modal.remote import RemoteReconstruction, RemoteSegmentation
    from roomfittr_pipeline.backends import InstanceMask

    reconstruct = RemoteReconstruction(function_name=args.gpu)
    segment = None if args.no_segment else RemoteSegmentation()

    # S4 gets a subset. `pipeline.run` segments whatever frames it is given, so
    # the subset is taken by handing it a directory with fewer files would be
    # wrong -- instead the backend is wrapped to slice its own input.
    if segment is not None and args.segment_frames < len(model_frames):
        step = max(1, len(model_frames) // args.segment_frames)
        keep = set(model_frames[::step][: args.segment_frames])
        inner = segment  # bound here so the closure below is not Optional

        class _Subset:
            name = "remote-sam3-subset"

            def segment(
                self, frame_paths: list[Path], prompts: dict[str, list[str]]
            ) -> list[InstanceMask]:
                # Masks must stay aligned with *all* frames, so the skipped
                # ones come back empty rather than missing.
                import numpy as np

                subset = [p for p in frame_paths if p in keep]
                found = inner.segment(subset, prompts)
                positions = {p: i for i, p in enumerate(frame_paths)}
                out: list[InstanceMask] = []
                for instance in found:
                    full = np.zeros((len(frame_paths), *instance.masks.shape[1:]), dtype=bool)
                    for local_i, path in enumerate(subset):
                        full[positions[path]] = instance.masks[local_i]
                    out.append(
                        type(instance)(
                            track_id=instance.track_id,
                            label=instance.label,
                            label_group=instance.label_group,
                            score=instance.score,
                            masks=full,
                        )
                    )
                return out

        segment_backend: Any = _Subset()
        print(f"S4        : segmenting {len(keep)} of {len(model_frames)} frames")
    else:
        segment_backend = segment

    t1 = time.perf_counter()
    result = pipeline.run(
        video,
        work,
        reconstruct=reconstruct,
        segment=segment_backend,
        pipeline_version=PIPELINE_VERSION,
        target_frames=args.frames,
    )
    timings["s3_s9"] = time.perf_counter() - t1
    timings["s3_gpu"] = reconstruct.last_elapsed_s or 0.0

    peak_gb = (reconstruct.last_peak_vram_bytes or 0) / 1e9
    print(f"S3 GPU    : {timings['s3_gpu']:.1f}s, peak {peak_gb:.2f} GB")
    print(f"S3-S9     : {timings['s3_s9']:.1f}s total")
    print(f"backend   : {result.reconstruction_backend}")
    print(f"scale     : {result.scale.factor:.4f} ({result.scale.confidence})")
    ceiling_mm = result.room_model["ceiling_height_mm"]
    print(f"area      : {result.geometry.area_m2:.2f} m2, ceiling {ceiling_mm} mm")
    print(f"objects   : {len(result.objects_file.get('objects', []))}")
    if result.warnings:
        print("warnings  : " + "; ".join(result.warnings))

    # S8: the shell, exported and size-checked against 9.2's budget.
    glb_path = work / "shell.glb"
    mesh = shell.build_shell(result.geometry)
    size = shell.export_glb(mesh, glb_path)
    shell.check_budget(size)
    print(f"shell     : {size:,} bytes glb ({size / 1e6:.3f} MB)")

    # Artefacts, under 2.4's key layout.
    room_id, scan_id = entry["room_id"], args.capture
    written = []
    for stage, filename, payload in (
        (Stage.GEOMETRY, "room.json", json.dumps(result.room_model, indent=2).encode()),
        (Stage.FUSE, "objects.json", json.dumps(result.objects_file, indent=2).encode()),
    ):
        key = storage.artefact_key(
            room_id=room_id,
            scan_id=scan_id,
            pipeline_version=PIPELINE_VERSION,
            stage=stage,
            filename=filename,
        )
        store.put_bytes(key, payload, content_type="application/json")
        written.append(key)
    glb_key = storage.artefact_key(
        room_id=room_id,
        scan_id=scan_id,
        pipeline_version=PIPELINE_VERSION,
        stage=Stage.SHELL,
        filename="shell.glb",
    )
    store.put_file(glb_key, glb_path, content_type="model/gltf-binary")
    written.append(glb_key)
    print("artefacts :")
    for key in written:
        print(f"  {key}")

    # The prediction the harness scores.
    out_dir = EVAL / "runs" / args.run
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{args.capture}.json").write_text(
        json.dumps(result.room_model, indent=2), encoding="utf-8"
    )
    meta_path = out_dir / "meta.json"
    existing = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    existing[args.capture] = {
        "backend": result.reconstruction_backend,
        "gpu_function": args.gpu,
        "frames": result.frames_used,
        "timings_s": {k: round(v, 2) for k, v in timings.items()},
        "peak_vram_bytes": reconstruct.last_peak_vram_bytes,
        "scale_factor": result.scale.factor,
        "scale_confidence": str(result.scale.confidence),
        "warnings": result.warnings,
        "artefacts": written,
    }
    meta_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    print(f"prediction: {out_dir / (args.capture + '.json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
