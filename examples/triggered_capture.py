"""Interactive one-trigger/one-frame capture example.

Press Enter for one photo. Type ``q`` and Enter to stop.
"""

from __future__ import annotations

import argparse
import os

from edsdk.camera_controller import CameraController
from edsdk.triggered_capture import CameraBusyError


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--transfer",
        choices=("memory", "deferred_card"),
        default="memory",
    )
    parser.add_argument("--save-dir", default="triggered-captures")
    parser.add_argument("--tv")
    parser.add_argument("--av", type=float)
    parser.add_argument("--iso", type=int)
    args = parser.parse_args()

    defaults = {
        key: value
        for key, value in {
            "tv": args.tv,
            "av": args.av,
            "iso": args.iso,
        }.items()
        if value is not None
    }
    os.makedirs(args.save_dir, exist_ok=True)

    with CameraController(
        protected=True,
        save_dir=args.save_dir,
    ) as camera:
        mode = camera.arm_triggered_capture(
            transfer=args.transfer,
            defaults=defaults,
            max_deferred_frames=10,
        )
        try:
            print("Armed. Enter=capture, q=finish")
            while True:
                command = input("> ").strip().lower()
                if command == "q":
                    break
                try:
                    frame = mode.trigger(wait=True)
                except CameraBusyError:
                    print("BUSY")
                    continue
                print(
                    frame.trigger_id,
                    [asset.filename for asset in frame.assets],
                )

            if args.transfer == "deferred_card":
                frames = mode.drain(output="files")
                print(
                    "Downloaded:",
                    [asset.path for frame in frames for asset in frame.assets],
                )
        finally:
            mode.disarm(leave_on_card=args.transfer == "deferred_card")


if __name__ == "__main__":
    main()
