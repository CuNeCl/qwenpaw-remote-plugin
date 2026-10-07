"""Test harness for the Remote SSH plugin.

The plugin's store imports ``qwenpaw.constant.WORKING_DIR``, which is only
available inside a QwenPaw host. A minimal stub is injected so the pure
logic can be tested standalone.
"""

import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if "qwenpaw" not in sys.modules:
    qwenpaw_stub = types.ModuleType("qwenpaw")
    constant_stub = types.ModuleType("qwenpaw.constant")
    constant_stub.WORKING_DIR = ROOT / ".pytest-working-dir"
    qwenpaw_stub.constant = constant_stub
    sys.modules["qwenpaw"] = qwenpaw_stub
    sys.modules["qwenpaw.constant"] = constant_stub
