"""Local web interface for peloton_picker.

Serves a small page on 127.0.0.1 where you set your preferences and it hands back
one class you haven't taken. Bound to localhost only — nothing is exposed to the
network, and your credentials never reach the browser.
"""

from __future__ import annotations

import json
import random
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Class Picker</title>
<style>
  :root {
    --bg: #f6f6f4; --panel: #fff; --ink: #1a1a1a; --muted: #6b6b6b;
    --line: #e2e2dd; --accent: #d1354b; --accent-ink: #fff; --chip: #f0efec;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #141416; --panel: #1d1d20; --ink: #f2f2f0; --muted: #9a9a97;
      --line: #2e2e33; --accent: #e8556a; --accent-ink: #1a1a1a; --chip: #26262b;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 32px 20px 64px; background: var(--bg); color: var(--ink);
    font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  }
  .wrap { max-width: 640px; margin: 0 auto; }
  h1 { font-size: 26px; margin: 0 0 4px; letter-spacing: -0.02em; }
  .sub { color: var(--muted); margin: 0 0 28px; font-size: 14px; }
  .card {
    background: var(--panel); border: 1px solid var(--line); border-radius: 14px;
    padding: 20px; margin-bottom: 18px;
  }
  label { display: block; font-weight: 600; font-size: 13px; margin-bottom: 8px;
          text-transform: uppercase; letter-spacing: 0.04em; color: var(--muted); }
  .row { margin-bottom: 20px; }
  .row:last-child { margin-bottom: 0; }
  select, input[type=number] {
    width: 100%; padding: 10px 12px; border-radius: 9px; border: 1px solid var(--line);
    background: var(--bg); color: var(--ink); font: inherit;
  }
  .chips { display: flex; flex-wrap: wrap; gap: 8px; }
  .chip {
    padding: 8px 14px; border-radius: 999px; border: 1px solid var(--line);
    background: var(--chip); cursor: pointer; user-select: none; font-size: 14px;
  }
  .chip.on { background: var(--accent); border-color: var(--accent); color: var(--accent-ink); font-weight: 600; }
  button.go {
    width: 100%; padding: 15px; border: 0; border-radius: 11px; background: var(--accent);
    color: var(--accent-ink); font: inherit; font-weight: 700; font-size: 16px; cursor: pointer;
  }
  button.go:disabled { opacity: 0.55; cursor: default; }
  button.again {
    margin-top: 14px; padding: 11px 16px; border-radius: 9px; border: 1px solid var(--line);
    background: transparent; color: var(--ink); font: inherit; cursor: pointer; width: 100%;
  }
  .result h2 { margin: 0 0 6px; font-size: 21px; letter-spacing: -0.01em; }
  .meta { color: var(--muted); font-size: 14px; margin-bottom: 14px; }
  .songs { list-style: none; padding: 0; margin: 14px 0 0; }
  .songs li { padding: 5px 0; border-top: 1px solid var(--line); font-size: 14px; }
  .badge {
    display: inline-block; background: var(--chip); border-radius: 6px;
    padding: 3px 9px; font-size: 12px; font-weight: 600; margin-right: 6px;
  }
  a.open {
    display: block; text-align: center; margin-top: 18px; padding: 13px;
    background: var(--accent); color: var(--accent-ink); border-radius: 10px;
    text-decoration: none; font-weight: 700;
  }
  .status { color: var(--muted); font-size: 14px; text-align: center; padding: 10px 0; }
  .err { color: var(--accent); font-size: 14px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>What should I ride today?</h1>
  <p class="sub">Picks one class you haven't taken yet.</p>

  <div class="card">
    <div class="row">
      <label for="track">Class type</label>
      <select id="track"></select>
    </div>
    <div class="row">
      <label>Length</label>
      <div class="chips" id="durations"></div>
    </div>
    <div class="row">
      <label for="instructor">Instructor</label>
      <select id="instructor"><option value="">Anyone</option></select>
    </div>
    <div class="row">
      <label for="minsongs">Minimum songs you like</label>
      <input type="number" id="minsongs" min="0" max="10" step="1">
    </div>
    <button class="go" id="go">Pick a class</button>
  </div>

  <div id="out"></div>
</div>

<script>
let CONFIG = null, seen = [], lastBody = null;

function chips(values, selected) {
  const box = document.getElementById('durations');
  box.innerHTML = '';
  values.forEach(v => {
    const el = document.createElement('div');
    el.className = 'chip' + (selected.includes(v) ? ' on' : '');
    el.textContent = v + ' min';
    el.dataset.value = v;
    el.onclick = () => el.classList.toggle('on');
    box.appendChild(el);
  });
}

function syncTrack() {
  const t = CONFIG.tracks[document.getElementById('track').value];
  chips(t.all_durations, t.durations);
  document.getElementById('minsongs').value = t.min_liked_songs;
}

fetch('/api/config').then(r => r.json()).then(cfg => {
  CONFIG = cfg;
  const sel = document.getElementById('track');
  Object.keys(cfg.tracks).forEach(name => {
    const o = document.createElement('option');
    o.value = name;
    o.textContent = name.charAt(0).toUpperCase() + name.slice(1);
    sel.appendChild(o);
  });
  sel.value = cfg.default_track;
  sel.onchange = syncTrack;
  const ins = document.getElementById('instructor');
  cfg.instructors.forEach(n => {
    const o = document.createElement('option');
    o.value = n; o.textContent = n;
    ins.appendChild(o);
  });
  syncTrack();
});

function collect() {
  return {
    track: document.getElementById('track').value,
    minutes: [...document.querySelectorAll('.chip.on')].map(c => +c.dataset.value),
    instructor: document.getElementById('instructor').value,
    min_liked_songs: +document.getElementById('minsongs').value,
  };
}

async function pick(body) {
  const out = document.getElementById('out');
  const btn = document.getElementById('go');
  btn.disabled = true;
  out.innerHTML = '<div class="card status">Looking through the catalogue… the first search of a class type takes a minute.</div>';
  try {
    const res = await fetch('/api/pick', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({...body, exclude: seen}),
    });
    const data = await res.json();
    if (data.error) {
      out.innerHTML = '<div class="card err">' + data.error + '</div>';
      return;
    }
    seen.push(data.ride_id);
    lastBody = body;
    render(data);
  } catch (e) {
    out.innerHTML = '<div class="card err">Something broke: ' + e + '</div>';
  } finally {
    btn.disabled = false;
  }
}

function render(k) {
  const badges = [];
  if (k.liked_songs.length) badges.push(k.liked_songs.length + ' songs you like');
  if (k.rating) badges.push(Math.round(k.rating * 100) + '% liked');
  if (k.difficulty) badges.push('difficulty ' + k.difficulty.toFixed(1));
  document.getElementById('out').innerHTML = `
    <div class="card result">
      <h2>${esc(k.title)}</h2>
      <div class="meta">${esc(k.instructor)} · ${k.duration_min} min · aired ${esc(k.aired)}</div>
      <div>${badges.map(b => '<span class="badge">' + esc(b) + '</span>').join('')}</div>
      ${k.liked_songs.length ? '<ul class="songs">' + k.liked_songs.map(s => '<li>♫ ' + esc(s) + '</li>').join('') + '</ul>' : ''}
      <a class="open" href="${k.url}" target="_blank" rel="noopener">Open in Peloton</a>
      <button class="again" id="again">Not this one — pick another</button>
    </div>`;
  document.getElementById('again').onclick = () => pick(lastBody);
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, c => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

document.getElementById('go').onclick = () => { seen = []; pick(collect()); };
</script>
</body>
</html>
"""

ALL_DURATIONS = [5, 10, 15, 20, 30, 45, 60]


def make_handler(pick_fn, config_fn):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # keep the terminal quiet

        def _send(self, code, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/":
                self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            elif self.path == "/api/config":
                self._send(200, json.dumps(config_fn()).encode(), "application/json")
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):
            if self.path != "/api/pick":
                self._send(404, b"not found", "text/plain")
                return
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self._send(400, b'{"error":"bad request"}', "application/json")
                return
            try:
                result = pick_fn(body)
            except Exception as exc:  # surfaced in the page rather than the terminal
                result = {"error": str(exc)}
            self._send(200, json.dumps(result).encode(), "application/json")

    return Handler


def serve(pick_fn, config_fn, port: int = 8765, open_browser: bool = True) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(pick_fn, config_fn))
    url = f"http://127.0.0.1:{port}/"
    print(f"Class picker running at {url}")
    print("Leave this window open while you use it. Ctrl-C to stop.")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.shutdown()
