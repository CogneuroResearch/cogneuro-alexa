#!/usr/bin/env python3
"""Report what this Piper build's Python API actually looks like.

Only needed if speech.py logs "piper Python API shape not recognised".
Run on the Pi with the venv active:  python inspect_piper.py
"""
import inspect
import os
import sys

model = os.environ.get("PIPER_MODEL") or (sys.argv[1] if len(sys.argv) > 1 else "")
if not model:
    sys.exit("set PIPER_MODEL or pass the .onnx path")

import piper
print("piper version:", getattr(piper, "__version__", "unknown"))

from piper import PiperVoice
voice = PiperVoice.load(model)
print("\npublic attributes:")
print(" ", [a for a in dir(voice) if not a.startswith("_")])

for name in ("synthesize", "synthesize_stream_raw", "synthesize_raw", "synthesize_wav"):
    fn = getattr(voice, name, None)
    if fn:
        try:
            print(f"\n{name}{inspect.signature(fn)}")
        except (TypeError, ValueError):
            print(f"\n{name}(?)")

fn = getattr(voice, "synthesize", None)
if fn:
    print("\ncalling synthesize('Test.') ...")
    try:
        out = fn("Test.")
        print("  returned:", type(out).__name__)
        if hasattr(out, "__iter__") and not isinstance(out, (bytes, bytearray, str)):
            first = next(iter(out), None)
            print("  first item:", type(first).__name__)
            if first is not None:
                print("  item attrs:", [a for a in dir(first) if not a.startswith("_")])
    except Exception as exc:
        print("  raised:", type(exc).__name__, exc)
