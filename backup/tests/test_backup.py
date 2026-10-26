import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error

SOURCE = Path(__file__).resolve().parents[1]

def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        for name in ('recorder.py', 'interface.py', 'index.html'):
            shutil.copy2(SOURCE / name, self.base / name)
        self.recorder = load(self.base / 'recorder.py', 'test_recorder')

    def tearDown(self):
        self.temp.cleanup()

    def test_processing_lock_serializes_and_releases_workers(self):
        script = '''import sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from recorder import processing_lock
with processing_lock(Path(sys.argv[2])):
 print('locked',flush=True)
 time.sleep(float(sys.argv[3]))
'''
        args = [sys.executable, '-c', script, str(self.base), str(self.base / '.processing.lock')]
        first = subprocess.Popen(args + ['0.7'], stdout=subprocess.PIPE, text=True)
        self.assertEqual(first.stdout.readline().strip(), 'locked')
        second = subprocess.Popen(args + ['0'], stdout=subprocess.PIPE, text=True)
        try:
            time.sleep(0.2)
            self.assertIsNone(second.poll(), 'Second worker must wait for the first')
            self.assertEqual(first.wait(timeout=10), 0)
            self.assertEqual(second.communicate(timeout=10)[0].strip(), 'locked')
            self.assertEqual(second.returncode, 0)
        finally:
            for proc in (first, second):
                if proc.poll() is None:
                    proc.kill()
                proc.wait()
                if proc.stdout:
                    proc.stdout.close()

    def test_stop_file_stops_active_capture(self):
        self.recorder.STOP_FILE = self.base / 'channel.stop'
        errors = []
        def capture():
            try:
                self.recorder.run([sys.executable, '-c', 'import time; time.sleep(60)'], self.base / 'capture.log')
            except RuntimeError as exc:
                errors.append(exc)  # interrupted capture queues its retained original in watch()
        thread = threading.Thread(target=capture)
        thread.start()
        deadline = time.monotonic() + 10
        while self.recorder.ACTIVE is None and time.monotonic() < deadline:
            time.sleep(.02)
        self.recorder.STOP_FILE.touch()
        thread.join(timeout=15)
        self.assertFalse(thread.is_alive())
        self.assertIsNone(self.recorder.ACTIVE)

    def test_api_auth_settings_and_path_restrictions(self):
        ui = load(self.base / 'interface.py', 'test_interface')
        server = ui.ThreadingHTTPServer(('127.0.0.1', 0), ui.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = 'http://127.0.0.1:' + str(server.server_address[1])
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def request(path, data=None, token=None, host='127.0.0.1:8766', origin=None):
            headers = {'Host': host}
            if origin:
                headers['Origin'] = origin
            if token:
                headers['X-Recorder-Token'] = token
            if data is not None:
                headers['Content-Type'] = 'application/json'
                data = json.dumps(data).encode()
            return opener.open(urllib.request.Request(url+path, data=data, headers=headers), timeout=5)
        def status(path, expected, **kwargs):
            try:
                with request(path, **kwargs) as response:
                    self.assertEqual(response.status, expected)
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, expected)
                exc.close()
        try:
            status('/api/action', 403, data={'action':'add', 'channel':'example'})
            status('/api/session', 403, origin='https://example.com')
            status('/api/status', 403, host='evil.example:8766')
            status('/files/../../interface.py', 404)
            status('/api/action', 200, token=ui.TOKEN, data={'action':'add','channel':'example'})
            self.assertIn('example', json.loads(ui.PREFS.read_text(encoding='utf-8')))
            ui.WATCHERS['example'] = type('Fake', (), {'poll':lambda self: None})()
            status('/api/action', 200, token=ui.TOKEN, data={'action':'stop','channel':'example'})
            self.assertTrue((ui.ROOT/'example.stop').exists())
            self.recorder.save(ui.ROOT, 'complete', channel='café')
            self.assertEqual(json.loads((ui.ROOT/'status.json').read_text(encoding='utf-8'))['channel'], 'café')
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

if __name__ == '__main__':
    unittest.main()
