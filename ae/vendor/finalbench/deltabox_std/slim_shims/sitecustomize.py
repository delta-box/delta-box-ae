"""Slim-worker import shims.

When DELTABOX_SLIM_SHIMS=1, keep heavyweight, checkpoint-external state out of
the worker process. The real CodeIndex lives in index_sidecar.py; this shim only
prevents moatless.index.__init__ from importing the real code_index module while
SearchTree/action classes are imported inside the checkpoint target.
"""
from __future__ import annotations

import os
import sys
import types


if os.environ.get("DELTABOX_SLIM_SHIMS") == "1":
    code_index = types.ModuleType("moatless.index.code_index")

    class CodeIndex:  # pragma: no cover - structural placeholder only.
        pass

    code_index.CodeIndex = CodeIndex
    sys.modules.setdefault("moatless.index.code_index", code_index)
