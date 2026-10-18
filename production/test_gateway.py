from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from production.gateway import nginx_config, public_apps_for_release
from production.runtime import CATALOG


class GatewaySyntaxTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which('nginx'), 'Nginx is exercised in clean Ubuntu CI')
    def test_real_nginx_parser_accepts_all_native_and_prefixed_routes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = nginx_config(list(CATALOG), root, root / 'auth',
                                  listen='127.0.0.1:18890', loopback=False)
            config = root / 'nginx.conf'
            config.write_text(f'pid {root}/pid;\nerror_log {root}/error.log;\n'
                              'events {}\nhttp { access_log off;\n' + server + '}\n')
            result = subprocess.run(['nginx', '-t', '-c', str(config)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
