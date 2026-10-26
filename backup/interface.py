#!/usr/bin/env python3
"""Local control panel for the independent backup recorder."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, unquote
import importlib.util
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import site
os.environ["PATH"] = str(Path(site.USER_BASE) / "bin") + os.pathsep + os.environ.get("PATH", "")
import threading
import webbrowser

TOOLS_PATH = Path(__file__).resolve().parent / "ffmpeg-path.txt"
if os.name == "nt" and TOOLS_PATH.exists():
    os.environ["PATH"] = TOOLS_PATH.read_text(encoding="utf-16").strip() + os.pathsep + os.environ.get("PATH", "")

BASE = Path(__file__).resolve().parent
ROOT = BASE / 'Recordings'
ROOT.mkdir(exist_ok=True)
TOKEN = secrets.token_hex(32)
PAGE_ORIGIN = 'https://ewanders1-web.github.io'
WATCHERS = {}
MUTEX = threading.Lock()
PREFS = BASE / 'streamers.json'
CHANNELS = json.loads(PREFS.read_text(encoding="utf-8")) if PREFS.exists() else ['whistleface']

def launch(args, log):
    with open(log, 'ab') as out:
        return subprocess.Popen([sys.executable, str(BASE / 'recorder.py')] + args,
                                stdout=out, stderr=out, start_new_session=True)

def snapshot():
    sessions = []
    for folder in sorted(ROOT.iterdir(), reverse=True):
        if not folder.is_dir():
            continue
        try:
            state = json.loads((folder / 'status.json').read_text(encoding="utf-8"))
            state['id'] = folder.name
            source = folder / 'original.ts'
            state['bytes'] = source.stat().st_size if source.exists() else 0
            manifest = folder / 'music-parts' / 'manifest.json'
            state['files'] = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else []
            sessions.append(state)
        except (OSError, ValueError):
            continue
    streamers = []
    for channel in CHANNELS:
        proc = WATCHERS.get(channel)
        active = bool(proc and proc.poll() is None)
        log = ROOT / (channel + '-watcher.log')
        message = ''
        if log.exists():
            with open(log, 'rb') as f:
                f.seek(max(0, log.stat().st_size - 1500))
                message = f.read().decode(errors='replace').strip().split('\n')[-1]
        streamers.append(dict(channel=channel, watching=active, message=message))
    return dict(streamers=streamers, sessions=sessions,
                dependencies={x: bool(shutil.which(x)) for x in ['ffmpeg', 'ffprobe', 'streamlink']},
                demucs=importlib.util.find_spec('demucs') is not None,
                freeGB=round(shutil.disk_usage(ROOT).free / 1e9, 1))

class Handler(BaseHTTPRequestHandler):
    def end_headers(self):
        origin = self.headers.get('Origin')
        if origin == PAGE_ORIGIN:
            self.send_header('Access-Control-Allow-Origin', origin)
            self.send_header('Vary', 'Origin')
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type, X-Recorder-Token')
            self.send_header('Access-Control-Allow-Private-Network', 'true')
        super().end_headers()

    def do_OPTIONS(self):
        if not self.allowed() or self.headers.get('Origin') != PAGE_ORIGIN:
            return self.reply({'error': 'Origin not allowed'}, 403)
        self.send_response(204)
        self.end_headers()

    def allowed(self):
        return self.headers.get('Host') in ('127.0.0.1:8766', 'localhost:8766')

    def reply(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self.allowed():
            return self.reply({'error': 'Invalid host'}, 403)
        path = unquote(urlparse(self.path).path)
        if path == '/api/session':
            origin = self.headers.get('Origin')
            if origin and origin not in (PAGE_ORIGIN, 'http://127.0.0.1:8766', 'http://localhost:8766'):
                return self.reply({'error': 'Origin not allowed'}, 403)
            return self.reply({'token': TOKEN})
        if path == '/api/status':
            with MUTEX:
                return self.reply(snapshot())
        if path == '/':
            body = (BASE / 'index.html').read_text(encoding="utf-8").replace('__TOKEN__', TOKEN).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path.startswith('/files/'):
            file = (ROOT / path[len('/files/'):]).resolve()
            if ROOT.resolve() not in file.parents or not file.is_file():
                return self.reply({'error': 'File not found'}, 404)
            self.send_response(200)
            self.send_header('Content-Type', 'audio/ogg' if file.suffix == '.ogg' else 'application/octet-stream')
            self.send_header('Content-Disposition', 'attachment; filename="' + file.name.replace('"', '') + '"')
            self.send_header('Content-Length', str(file.stat().st_size))
            self.end_headers()
            with file.open('rb') as src:
                shutil.copyfileobj(src, self.wfile)
            return
        self.reply({'error': 'Not found'}, 404)

    def do_POST(self):
        if not self.allowed() or self.headers.get('X-Recorder-Token') != TOKEN:
            return self.reply({'error': 'Reload the recorder page'}, 403)
        try:
            length = int(self.headers.get('Content-Length', 0))
            if not 0 < length < 4096:
                raise ValueError('Invalid request')
            data = json.loads(self.rfile.read(length))
            with MUTEX:
                action = data.get('action')
                channel = str(data.get('channel', '')).strip().lower()
                if action in ('add', 'start', 'stop', 'remove'):
                    if not re.fullmatch('[a-z0-9_]{1,25}', channel):
                        raise ValueError('Enter a channel name, such as whistleface')
                    proc = WATCHERS.get(channel)
                    active = proc and proc.poll() is None
                    if action == 'add' and channel not in CHANNELS:
                        CHANNELS.append(channel)
                    elif action == 'start':
                        if not all(shutil.which(x) for x in ['ffmpeg', 'ffprobe', 'streamlink']) or importlib.util.find_spec('demucs') is None:
                            raise ValueError('Run Install.command (Mac) or Install.cmd (Windows), then reopen this interface.')
                        if channel not in CHANNELS:
                            raise ValueError('Add this streamer first')
                        if not active:
                            (ROOT / (channel + '.stop')).unlink(missing_ok=True)
                            WATCHERS[channel] = launch(['watch', channel], ROOT / (channel + '-watcher.log'))
                    elif action == 'stop' and active:
                        # A stop file works without POSIX console signals on Windows.
                        (ROOT / (channel + '.stop')).touch()
                    elif action == 'remove':
                        if active:
                            raise ValueError('Stop watching before removing this streamer')
                        if channel in CHANNELS:
                            CHANNELS.remove(channel)
                    PREFS.write_text(json.dumps(CHANNELS), encoding="utf-8")
                elif action == 'retry':
                    folder = (ROOT / str(data.get('session', ''))).resolve()
                    if folder.parent != ROOT.resolve() or not folder.is_dir():
                        raise ValueError('Session not found')
                    state = json.loads((folder / 'status.json').read_text(encoding="utf-8"))
                    if state['state'] not in ('processing failed', 'recorded; ready to process'):
                        raise ValueError('This session is already recording, processing or complete')
                    state['state'] = 'queued for processing'
                    (folder / 'status.json').write_text(json.dumps(state), encoding="utf-8")
                    launch(['process', str(folder)], folder / 'worker.log')
                else:
                    raise ValueError('Unknown action')
            self.reply({'ok': True})
        except Exception as exc:
            self.reply({'error': str(exc)}, 400)

if __name__ == '__main__':
    if os.name == "nt":
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)
    server = ThreadingHTTPServer(('127.0.0.1', 8766), Handler)
    print('Backup recorder: http://127.0.0.1:8766 — keep this window open.', flush=True)
    threading.Timer(0.6, lambda: webbrowser.open('http://127.0.0.1:8766')).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        for channel, proc in WATCHERS.items():
            if proc.poll() is None:
                (ROOT / (channel + '.stop')).touch()
    finally:
        server.server_close()
        if os.name == "nt":
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)

