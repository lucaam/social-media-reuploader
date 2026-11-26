"""The admin console (``src/static/app.js``) driven in a JavaScript runtime.

The repository has no browser test tooling, so these tests load the real
``app.js`` into a small stand-in for the browser (elements, events, ``fetch``,
``WebSocket`` and timers) and use it the way an administrator would. The JSON
the fake ``fetch`` returns is taken from the real API (a ``TestClient`` on a
seeded database), so the console is exercised against the server's shapes.

deno is installed with the Python requirements (yt-dlp's JavaScript runtime);
node is used when deno is missing. Without either the tests are skipped.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from src import db, gui
from src.gui_auth import AuthSettings

TOKEN = "admin-token-0123456789abcdef"
BEARER = {"Authorization": f"Bearer {TOKEN}"}
APP_JS = Path(gui.STATIC_DIR) / "app.js"
INDEX_HTML = Path(gui.STATIC_DIR) / "index.html"
# What the console loads at start and on each tab (bare paths, as app.js asks).
DEFAULT_GETS = (
    "config",
    "api/me",
    "api/worker",
    "api/stats",
    "api/aggregates?hours=24&top=10",
    "api/requests?limit=25",
    "api/queue?limit=100",
    "api/rate_limits",
    "api/events?limit=50",
    "api/users?limit=200",
    "api/config",
    "health",
)

# A minimal browser: DOM elements, fetch, WebSocket and controllable timers.
PRELUDE = r"""
'use strict'
const SPA = globalThis.__SPA__
const realSetTimeout = globalThis.setTimeout
const consoleErrors = []
const realConsoleError = console.error.bind(console)
console.error = (...args) => {
  consoleErrors.push(args.map((arg) => String(arg && arg.stack ? arg.stack : arg)).join(' '))
  realConsoleError(...args)
}

function exitWith (code) {
  if (typeof Deno !== 'undefined') Deno.exit(code)
  else process.exit(code)
}

class FakeNode {}

class FakeText extends FakeNode {
  constructor (text) {
    super()
    this.data = String(text)
    this.parentElement = null
  }

  get textContent () { return this.data }
  remove () { detach(this) }
}

function detach (node) {
  const parent = node.parentElement
  if (parent) {
    parent.childNodes = parent.childNodes.filter((child) => child !== node)
    node.parentElement = null
  }
}

class FakeElement extends FakeNode {
  constructor (tag, id) {
    super()
    this.tagName = String(tag).toUpperCase()
    this.id = id || ''
    this.childNodes = []
    this.parentElement = null
    this.listeners = {}
    this.attributes = {}
    this.dataset = {}
    this.hidden = false
    this.disabled = false
    this.className = ''
    this.value = ''
    this.title = ''
    this.ownText = ''
    const classes = new Set()
    this.classList = {
      add: (...names) => names.forEach((name) => classes.add(name)),
      remove: (...names) => names.forEach((name) => classes.delete(name)),
      contains: (name) => classes.has(name),
      toggle: (name, force) => {
        const on = force === undefined ? !classes.has(name) : Boolean(force)
        if (on) classes.add(name)
        else classes.delete(name)
        return on
      }
    }
  }

  get children () { return this.childNodes.filter((node) => node instanceof FakeElement) }
  get firstElementChild () { return this.children[0] || null }
  get options () { return this.children.filter((node) => node.tagName === 'OPTION') }
  get textContent () { return this.ownText + this.childNodes.map((node) => node.textContent).join('') }
  set textContent (value) {
    for (const node of this.childNodes) node.parentElement = null
    this.childNodes = []
    this.ownText = value === null || value === undefined ? '' : String(value)
  }

  append (...items) {
    for (const item of items) {
      const node = item instanceof FakeNode ? item : new FakeText(item)
      detach(node)
      node.parentElement = this
      this.childNodes.push(node)
    }
  }

  replaceChildren (...items) {
    this.textContent = ''
    this.append(...items)
  }

  remove () { detach(this) }

  setAttribute (name, value) {
    this.attributes[name] = String(value)
    if (name === 'value') this.value = String(value)
  }

  getAttribute (name) { return name in this.attributes ? this.attributes[name] : null }
  addEventListener (type, listener) { (this.listeners[type] ||= []).push(listener) }

  dispatch (type, extra) {
    const event = { type, target: this, defaultPrevented: false, preventDefault () { this.defaultPrevented = true }, ...extra }
    for (const listener of this.listeners[type] || []) listener(event)
    return event
  }

  click () { if (!this.disabled) this.dispatch('click') }
  focus () { document.activeElement = this }

  contains (node) {
    for (let current = node; current; current = current.parentElement) {
      if (current === this) return true
    }
    return false
  }

  descendants () {
    const found = []
    const walk = (node) => {
      for (const child of node.children) {
        found.push(child)
        walk(child)
      }
    }
    walk(this)
    return found
  }

  querySelector (selector) {
    if (selector === 'button:last-child') {
      const last = this.children[this.children.length - 1]
      return last && last.tagName === 'BUTTON' ? last : null
    }
    throw new Error('fake DOM: unsupported selector ' + selector)
  }
}

const byId = new Map()
const hiddenAtStart = new Set(SPA.hidden)
const fakeDocument = {
  readyState: 'complete',
  hidden: false,
  activeElement: null,
  body: new FakeElement('body'),
  listeners: {},
  getElementById (id) {
    let node = byId.get(id)
    if (!node) {
      node = new FakeElement('div', id)
      node.hidden = hiddenAtStart.has(id)
      const holder = new FakeElement('div')
      holder.append(node)
      this.body.append(holder)
      byId.set(id, node)
    }
    return node
  },
  createElement (tag) { return new FakeElement(tag) },
  querySelectorAll () { return [] },
  addEventListener (type, listener) { (this.listeners[type] ||= []).push(listener) }
}

const fakeWindow = {
  Chart: undefined,
  listeners: {},
  addEventListener (type, listener) { (this.listeners[type] ||= []).push(listener) },
  dispatch (type) { for (const listener of this.listeners[type] || []) listener({ type }) }
}
const fakeLocation = { href: 'http://gui.test/', protocol: 'http:', hash: SPA.hash || '' }
const fakeHistory = {
  replaceState (state, title, url) {
    if (typeof url === 'string' && url.startsWith('#')) fakeLocation.hash = url
  }
}

let clock = 0
let timerSeq = 0
const timers = new Map()

const routes = { ...SPA.routes }
const h = {
  calls: [],
  unexpected: [],
  sockets: [],
  data: SPA.data,
  allowConsoleErrors: false,
  async settle () {
    for (let i = 0; i < 20; i++) await new Promise((resolve) => realSetTimeout(resolve, 0))
  },
  async advance (ms) {
    const until = clock + ms
    for (;;) {
      const due = [...timers.entries()]
        .filter(([, timer]) => timer.at <= until)
        .sort((a, b) => a[1].at - b[1].at || a[0] - b[0])[0]
      if (!due) break
      timers.delete(due[0])
      clock = due[1].at
      due[1].fn()
      await h.settle()
    }
    clock = until
    await h.settle()
  },
  route (key, reply) { routes[key] = reply },
  fetched (path) { return h.calls.filter((call) => call.path.split('?')[0] === path).length },
  el (id) { return fakeDocument.getElementById(id) },
  text (id) { return fakeDocument.getElementById(id).textContent },
  labels (id) { return h.el(id).children.map((node) => node.textContent) },
  find (root, predicate) {
    return (typeof root === 'string' ? h.el(root) : root).descendants().find(predicate) || null
  },
  button (root, label) {
    const found = h.find(root, (node) => node.tagName === 'BUTTON' && node.textContent === label)
    if (!found) throw new Error('no button "' + label + '" in ' + (typeof root === 'string' ? root : root.tagName))
    return found
  },
  socket () { return h.sockets[h.sockets.length - 1] },
  goto (hash) {
    fakeLocation.hash = hash
    fakeWindow.dispatch('hashchange')
  },
  eq (actual, expected, message) {
    const got = JSON.stringify(actual)
    const want = JSON.stringify(expected)
    if (got !== want) throw new Error((message || 'values differ') + ': expected ' + want + ', got ' + got)
  },
  ok (value, message) {
    if (!value) throw new Error(message || 'assertion failed')
  }
}

class FakeWebSocket {
  constructor (url) {
    this.url = url
    this.readyState = FakeWebSocket.CONNECTING
    this.listeners = {}
    h.sockets.push(this)
  }

  addEventListener (type, listener) { (this.listeners[type] ||= []).push(listener) }
  emit (type, event) { for (const listener of this.listeners[type] || []) listener(event) }

  open () {
    this.readyState = FakeWebSocket.OPEN
    this.emit('open', {})
  }

  receive (message) { this.emit('message', { data: JSON.stringify(message) }) }

  close () {
    if (this.readyState === FakeWebSocket.CLOSED) return
    this.readyState = FakeWebSocket.CLOSED
    this.emit('close', {})
  }
}
FakeWebSocket.CONNECTING = 0
FakeWebSocket.OPEN = 1
FakeWebSocket.CLOSING = 2
FakeWebSocket.CLOSED = 3

async function fakeFetch (path, init) {
  const method = (init && init.method) || 'GET'
  const body = init && init.body !== undefined ? JSON.parse(init.body) : undefined
  h.calls.push({ path, method, body })
  const key = method + ' ' + String(path).split('?')[0]
  let reply = routes[key]
  if (typeof reply === 'function') reply = reply({ path, method, body })
  if (!reply) {
    h.unexpected.push(method + ' ' + path)
    reply = { status: 404, body: { detail: 'Not Found' } }
  }
  const status = reply.status || 200
  const text = JSON.stringify(reply.body === undefined ? null : reply.body)
  return { ok: status >= 200 && status < 300, status, text: async () => text }
}

const browser = {
  Node: FakeNode,
  document: fakeDocument,
  window: fakeWindow,
  location: fakeLocation,
  history: fakeHistory,
  WebSocket: FakeWebSocket,
  fetch: fakeFetch,
  setTimeout (fn, ms) {
    timerSeq += 1
    timers.set(timerSeq, { at: clock + Math.max(0, Number(ms) || 0), fn })
    return timerSeq
  },
  clearTimeout (id) { timers.delete(id) },
  setInterval () { return 0 }, // the periodic safety nets never fire here
  clearInterval () {}
}
for (const [name, value] of Object.entries(browser)) {
  Object.defineProperty(globalThis, name, { value, configurable: true, writable: true })
}
"""

RUNNER = r"""
;(async () => {
  await h.settle()
  await scenario(h)
  await h.settle()
  if (h.unexpected.length) throw new Error('requests without a fixture: ' + h.unexpected.join(', '))
  if (consoleErrors.length && !h.allowConsoleErrors) throw new Error('console errors: ' + consoleErrors.join(' | '))
})().then(() => exitWith(0), (error) => {
  console.log('FAILED: ' + (error && error.stack ? error.stack : error))
  exitWith(1)
})
"""


@pytest.fixture(autouse=True)
def db_file(tmp_path, monkeypatch):
    path = tmp_path / "requests.db"
    monkeypatch.setenv("REQUESTS_DB", str(path))
    db.init_db()
    return path


@pytest.fixture
def api():
    app = gui.create_app(AuthSettings(secret_key="k" * 40, admin_token=TOKEN))
    return TestClient(app, headers=BEARER)


def _js_runtime() -> list[str] | None:
    """deno (preferred) or node; ``SPA_JS_RUNTIME=node`` forces node."""
    wanted = os.environ.get("SPA_JS_RUNTIME", "")
    deno = shutil.which("deno") or _next_to_python("deno")
    if deno and wanted in ("", "deno"):
        return [deno, "run", "--quiet", "--no-prompt", "--no-config"]
    node = shutil.which("node")
    return [node] if node and wanted in ("", "node") else None


def _next_to_python(name: str) -> str | None:
    candidate = Path(sys.executable).parent / name
    return str(candidate) if candidate.is_file() else None


def hidden_at_start() -> list[str]:
    """Ids of the elements index.html renders with the ``hidden`` attribute."""
    ids = []
    for tag in re.findall(r"<[a-zA-Z][^>]*>", INDEX_HTML.read_text(encoding="utf-8")):
        found = re.search(r'\sid="([^"]+)"', tag)
        if found and re.search(r"\shidden(?=[\s>/])", tag):
            ids.append(found.group(1))
    return ids


def snapshot(api: TestClient, *paths: str) -> dict[str, Any]:
    """Fake-fetch routes (``"GET api/x"`` -> status/body) from the real API."""
    routes = {}
    for path in paths:
        response = api.get("/" + path)
        routes["GET " + path.split("?", 1)[0]] = {
            "status": response.status_code,
            "body": response.json(),
        }
    return routes


def run_spa(
    tmp_path: Path,
    scenario: str,
    *,
    routes: dict[str, Any],
    hash: str = "",
    data: Any = None,
) -> str:
    """Run app.js plus ``scenario`` (the body of ``async (h) => {...}``)."""
    runtime = _js_runtime()
    if runtime is None:
        pytest.skip("no JavaScript runtime (deno or node) available")
    config = {"routes": routes, "hash": hash, "hidden": hidden_at_start(), "data": data}
    source = "\n".join(
        [
            "globalThis.__SPA__ = " + json.dumps(config) + ";",
            PRELUDE,
            APP_JS.read_text(encoding="utf-8"),
            ";\nconst scenario = async (h) => {\n" + scenario + "\n};",
            RUNNER,
        ]
    )
    script = tmp_path / "spa_scenario.js"
    script.write_text(source, encoding="utf-8")
    env = {
        **os.environ,
        "DENO_DIR": str(tmp_path / "deno"),
        "DENO_NO_UPDATE_CHECK": "1",
        "NO_COLOR": "1",
    }
    result = subprocess.run(
        [*runtime, str(script)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    return output


# ---------------------------------------------------------------------------
# The harness itself
# ---------------------------------------------------------------------------
def test_the_console_starts_and_renders_the_overview(tmp_path, api):
    db.create_request(1, "https://example.com/v/1")
    routes = snapshot(api, *DEFAULT_GETS)
    run_spa(
        tmp_path,
        """
        h.ok(!h.el('app').hidden, 'the console is shown')
        h.ok(h.el('loginOverlay').hidden, 'no login overlay')
        h.eq(h.text('statTotal'), '1', 'total requests')
        h.eq(h.text('userLabel'), 'Accesso: Amministratore (token)', 'user label')
        h.eq(h.socket().url, 'ws://gui.test/ws/updates', 'live updates socket')
        for (const path of ['api/worker', 'api/stats', 'api/aggregates']) {
          h.eq(h.fetched(path), 1, path + ' loaded once')
        }
        """,
        routes=routes,
    )


def test_a_failing_scenario_fails_the_test(tmp_path, api):
    routes = snapshot(api, *DEFAULT_GETS)
    with pytest.raises(AssertionError, match="expected"):
        run_spa(tmp_path, "h.eq(1, 2, 'numbers')", routes=routes)


# ---------------------------------------------------------------------------
# Live updates
# ---------------------------------------------------------------------------
def test_each_live_connection_resyncs_once_subscribed(tmp_path, api):
    for number in range(15):
        db.create_request(1, f"https://example.com/v/{number}")
    routes = snapshot(api, *DEFAULT_GETS)
    # The bot adds a request after the first HTTP loads, before the socket
    # subscribes: its event is published to nobody.
    db.create_request(1, "https://example.com/v/new")
    newer = snapshot(api, "api/stats", "api/aggregates?hours=24&top=10")
    run_spa(
        tmp_path,
        """
        h.eq(h.text('statTotal'), '15', 'first load')
        let socket = h.socket()
        socket.open()
        await h.settle()
        h.eq(h.text('liveBadge'), 'In tempo reale', 'live badge')
        Object.entries(h.data).forEach(([key, reply]) => h.route(key, reply))
        socket.receive({ kind: 'hello', last_event_id: 15 })
        await h.settle()
        h.eq(h.text('statTotal'), '16', 'resynced after the first subscription')
        // a reconnection resyncs as well
        socket.close()
        h.eq(h.text('liveBadge'), 'Non in tempo reale', 'offline badge')
        await h.advance(1000)
        h.ok(h.sockets.length === 2, 'reconnected')
        socket = h.socket()
        socket.open()
        socket.receive({ kind: 'hello', last_event_id: 16 })
        await h.settle()
        h.eq(h.fetched('api/stats'), 3, 'one load per subscription plus the first')
        """,
        routes=routes,
        data=newer,
    )


# ---------------------------------------------------------------------------
# Request detail
# ---------------------------------------------------------------------------
def failed_request(chat_id: int, url: str, code: str = "unavailable") -> int:
    request_id = db.create_request(chat_id, url)
    assert db.claim_request(request_id)
    assert db.mark_failed(request_id, error_code=code, error_message="boom")
    return request_id


def test_the_open_detail_follows_live_changes_with_its_actions(tmp_path, api):
    request_id = failed_request(7, "https://example.com/v/1")
    detail = f"api/requests/{request_id}"
    routes = snapshot(api, *DEFAULT_GETS, detail)
    assert db.requeue_requests([request_id], actor="gui:oauth:other") == [request_id]
    requeued = snapshot(api, detail)
    run_spa(
        tmp_path,
        f"""
        h.button('requestsBody', 'Dettagli').click()
        await h.settle()
        h.eq(h.text('modalTitle'), 'Richiesta #{request_id}', 'detail open')
        h.eq(h.labels('modalFooter'), ['Rimetti in coda', 'Chiudi'], 'failed request')
        h.socket().open()
        // another admin requeues the request: its event reaches this console
        Object.entries(h.data).forEach(([key, reply]) => h.route(key, reply))
        h.socket().receive({{ kind: 'events', last_id: 99, events: [
          {{ id: 99, type: 'request_status', request_id: {request_id}, status: 'queued', previous: 'failed' }}
        ] }})
        await h.advance(500)
        h.ok(h.text('modalBody').includes('In coda'), 'the body shows the new state')
        h.eq(h.labels('modalFooter'), ['Annulla la coda di questa chat…', 'Chiudi'], 'queued request')
        h.button('modalFooter', 'Annulla la coda di questa chat…').click()
        await h.settle()
        h.eq(h.text('modalTitle'), 'Annullare la coda della chat?', 'the action still works')
        """,
        routes=routes,
        hash="#requests",
        data=requeued,
    )


# ---------------------------------------------------------------------------
# System tab
# ---------------------------------------------------------------------------
def test_clearing_the_history_says_which_requests_are_kept(tmp_path, api):
    db.set_worker_status(
        {"pid": 1, "rate_limits": [[20, 30 * 86400]], "duplicate_window_seconds": 5400}
    )
    routes = snapshot(api, *DEFAULT_GETS)
    routes["POST api/history/clear"] = {
        "body": {
            "ok": True,
            "deleted": {"requests": 2, "request_events": 4, "updates": 9},
            "kept_seconds": 30 * 86400,
        }
    }
    run_spa(
        tmp_path,
        """
        h.el('clearHistoryBtn').click()
        await h.settle()
        const text = h.text('modalBody')
        h.ok(text.includes('richieste completate o fallite più recenti di 30 giorni'), text)
        h.ok(text.includes('limiti di frequenza'), text)
        h.button('modalFooter', 'Elimina lo storico').click()
        await h.settle()
        const posts = h.calls.filter((call) => call.method === 'POST').map((call) => call.path)
        h.eq(posts, ['api/history/clear'], 'one clear request')
        h.ok(h.text('toasts').includes('Storico eliminato: 2 richieste, 9 eventi.'), h.text('toasts'))
        """,
        routes=routes,
        hash="#system",
    )
