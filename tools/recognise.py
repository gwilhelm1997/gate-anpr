"""Run the Gate ANPR engine on image files and print what it reads.

Usage: python tools/recognise.py IMAGE [IMAGE ...] [--region L,T,R,B] [--detector NAME] [--ocr NAME]
Region values are percentages of the frame, e.g. --region 20,30,80,100.
"""

import argparse
import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "gate_anpr" / "app"))

from gate_anpr.decision import Frame, Rules, evaluate  # noqa: E402
from gate_anpr.recognition import RecognitionEngine, Region  # noqa: E402


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("images", nargs="+")
    ap.add_argument("--region", default="0,0,100,100")
    ap.add_argument("--detector", default="yolo-v9-t-512-license-plate-end2end")
    ap.add_argument("--ocr", default="cct-s-v2-global-model")
    ap.add_argument("--threads", type=int, default=2)
    ap.add_argument("--annotate", help="folder to write annotated images to")
    args = ap.parse_args()

    region = Region(*(float(v) / 100 for v in args.region.split(",")))
    engine = RecognitionEngine(args.detector, args.ocr, args.threads)
    await engine.start()
    print(f"engine: {engine.info()}")
    frames = []
    for i, name in enumerate(args.images):
        jpeg = pathlib.Path(name).read_bytes()
        width, height, reads, ms = await engine.recognise(jpeg, region)
        frames.append(Frame(i, float(i), width, height, reads))
        print(f"{name}: {width}x{height} {ms} ms")
        for r in reads:
            print(f"   {r.text!r:10} raw={r.raw!r} ocr={r.ocr_conf:.3f} det={r.det_conf:.3f} box={r.box}")
        if args.annotate and reads:
            best = max(reads, key=lambda r: r.area)
            frame_jpeg, plate_jpeg = await engine.annotate(jpeg, best.box, best.text, region)
            out = pathlib.Path(args.annotate)
            out.mkdir(parents=True, exist_ok=True)
            (out / f"{pathlib.Path(name).stem}_annotated.jpg").write_bytes(frame_jpeg)
            (out / f"{pathlib.Path(name).stem}_plate.jpg").write_bytes(plate_jpeg)
    print("verdict over all images as one sequence:", evaluate(frames, Rules()))
    engine.close()


if __name__ == "__main__":
    asyncio.run(main())
