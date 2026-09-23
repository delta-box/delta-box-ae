"""The shared controller must preserve live workers' JSON framing."""
import json
import unittest
from unittest.mock import patch

from backends.deltabox.gsd import sandbox_controller as sc


class AbortProtocolTests(unittest.TestCase):
    def test_live_json_and_replay_resynchronization_framing(self):
        controller = sc.SandboxController.__new__(sc.SandboxController)
        controller.current_epoch = 7
        for strict in ("0", "1"):
            with self.subTest(strict=strict), \
                 patch.dict(sc.os.environ, {"DELTABOX_REPLAY_STRICT_EPOCH": strict}), \
                 patch.object(sc.os, "open", return_value=17), \
                 patch.object(sc.os, "write") as write, \
                 patch.object(sc.os, "close") as close:
                controller._write_ctrl_abort_pending_all()
                frame = write.call_args.args[1]
                self.assertEqual(frame.startswith(b"\x1e"), strict == "1")
                self.assertEqual(json.loads(frame.lstrip(b"\x1e")),
                                 {"ctrl": "abort_pending_all", "_replay_epoch": 7})
                close.assert_called_once_with(17)
