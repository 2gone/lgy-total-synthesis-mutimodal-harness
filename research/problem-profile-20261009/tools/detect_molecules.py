"""[Copied unchanged from e293-expression-review/tools/display_overlay/detect_molyolo.py (2026-10-15) so that the chain can box drawings on any
machine; the copy is kept identical to its source.]

MolYOLO on scheme images, for the review workbench's display layer.

Run with an interpreter that has torch. Two ways to get a detector: the handoff's
vendored YOLOv10 runtime (`--runtime`), or -- when no runtime is given -- the
`ultralytics` the interpreter already has, which loads the YOLO11 MolDet weights
(`moldet_yolo11l_640_general.pt`, the one the five-paper rebind used). It only
detects: every box it reports is a detector output with its confidence, in the
image's own pixels, and nothing downstream treats one as a fact about the paper.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--runtime",
        type=Path,
        default=None,
        help="the vendored molyolo_runtime directory; omit to use the installed ultralytics",
    )
    parser.add_argument("--weights", type=Path, required=True)
    parser.add_argument("--images", type=Path, nargs="+", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--imgsz", type=int, default=1024)
    parser.add_argument("--conf", type=float, default=0.25)
    parser.add_argument("--iou", type=float, default=0.5)
    parser.add_argument("--device", default="cpu")
    args = parser.parse_args()

    if args.runtime is not None:
        sys.path.insert(0, str(args.runtime))
    import ultralytics  # noqa: PLC0415 - only importable once the runtime is on the path

    if args.runtime is not None:
        from ultralytics import YOLOv10 as Detector  # noqa: PLC0415

        detector_name = "MolYOLO YOLOv10"
    else:
        from ultralytics import YOLO as Detector  # noqa: PLC0415

        detector_name = "MolDet YOLO11"
    model = Detector(str(args.weights))
    images = []
    for path in args.images:
        result = model.predict(
            str(path),
            imgsz=args.imgsz,
            conf=args.conf,
            iou=args.iou,
            device=args.device,
            # This runtime otherwise writes runs/detect/ into the working
            # directory -- which is wherever the build was started from.
            save_txt=False,
            save_conf=False,
            save_crop=False,
            project=str(args.out.parent / "ultralytics"),
            name="predict",
            exist_ok=True,
            save=False,
            verbose=False,
        )[0]
        height, width = result.orig_shape
        boxes = result.boxes.xyxy.detach().cpu().tolist()
        scores = result.boxes.conf.detach().cpu().tolist()
        images.append(
            {
                "image": path.name,
                "width_px": int(width),
                "height_px": int(height),
                "detections": [
                    {
                        "bbox_px": [round(float(value), 1) for value in box],
                        "confidence": round(float(score), 4),
                    }
                    for box, score in zip(boxes, scores, strict=True)
                ],
            }
        )
        print(f"{path.name}: {len(images[-1]['detections'])} boxes", flush=True)

    args.out.write_text(
        json.dumps(
            {
                "detector": detector_name,
                "ultralytics_version": ultralytics.__version__,
                "weights_sha256": sha256_file(args.weights),
                "imgsz": args.imgsz,
                "confidence_threshold": args.conf,
                "iou_threshold": args.iou,
                "device": args.device,
                "images": images,
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
