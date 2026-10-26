"""Carry pytest's B190 guard into ordinary spawned Python processes."""

import importlib.machinery
import os
import runpy
import sys

if os.environ.get("HARK_TEST_NETWORK_GUARD") == "1":
    from hark_test_guard import BOOTSTRAP, install

    install()
    # Preserve startup probes supplied by interrupt regressions.
    other_paths = [path for path in sys.path if os.path.realpath(path) != BOOTSTRAP]
    previous = importlib.machinery.PathFinder.find_spec("sitecustomize", other_paths)
    if previous is not None and previous.origin:
        runpy.run_path(previous.origin, run_name="sitecustomize")
