import os
os.environ.setdefault('LUNA_GUI_CHECKS', '0')  # hermetic: tests never probe the real desktop (test_gui_check manages it itself)
os.environ.setdefault('LUNA_LIVE_CHECKS', '0')  # hermetic: tests never contact a real Steam (test_live_probe uses a fake endpoint)
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class GeneratedLunaTests(unittest.TestCase):
    def test_luna_py_matches_fragments(self):
        result = subprocess.run([sys.executable, '-I', str(ROOT / 'scripts' / 'build-luna.py'), '--check'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0,
                         'scripts/luna.py is stale; run: python3 scripts/build-luna.py\n' + result.stderr)

    def test_generated_header(self):
        head = (ROOT / 'scripts' / 'luna.py').read_text(encoding='utf-8').splitlines()[:3]
        self.assertTrue(head[0].startswith('#!'))
        self.assertIn('GENERATED', head[1])


if __name__ == '__main__':
    unittest.main()
