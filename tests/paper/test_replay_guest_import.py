"""FC's minimal helper bundle must not import the host audit client."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class ReplayGuestImportTests(unittest.TestCase):
    def test_fc_four_file_payload_import_is_self_contained(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ('mock_llm_server.py', 'protocol.py', 'replay_driver.py', 'trajectory_index.py'):
                shutil.copy2(ROOT / 'ae/vendor/spr_payload' / name, root / name)
            subprocess.run([sys.executable, '-I', '-c',
                'import sys; sys.path.insert(0, sys.argv[1]); '
                'from replay_driver import _http_json, rewrite_model_base_url, strip_recorded_tree; '
                'assert "baseline_audit" not in sys.modules', tmp], check=True, capture_output=True)


if __name__ == '__main__':
    unittest.main()
