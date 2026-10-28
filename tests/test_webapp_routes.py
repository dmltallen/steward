"""Webapp route tests (S5).

Under test:

  1. The daemon serves the static client at the root: /, /index.html,
     /app.js and /style.css with the right content types and no auth.
  2. No built asset references any external URL (no http:// or https://
     anywhere in index.html, app.js or style.css).
  3. The markdown-lite renderer escapes a message containing <script> so it
     renders inert, and still renders real markdown, checked by running the
     actual app.js in Node (the renderer is pure JS, DOM-independent).
  4. The webapp API routes are Bearer-gated and work after pairing.
  5. Folder add/remove and index search/build follow the daemon auth and
     return citations. A fake index is injected so no embedder or model is
     touched.

Every HTTP test spins a real StewardServer on port 0 with a fake runtime
and tears it down in the fixture, so no daemon or port survives a run.
"""

from __future__ import annotations

import http.client
import json
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from steward.server import daemon as daemon_mod
from steward.webapp import serve as webapp_serve

WEBAPP_DIR = Path(__file__).resolve().parents[1] / "src" / "steward" / "webapp"

TOKENS = ["Hello ", "from ", "the ", "fake ", "backend."]


class FakeRuntime:
    """Minimal stand-in for the steward.runtime seam used by the daemon."""

    kind = "fake-runtime"
    url = "http://127.0.0.1:1"

    def __init__(self, tokens=None):
        self.tokens = list(tokens) if tokens else list(TOKENS)
        self.health_ok = True

    def health(self):
        return {"ok": self.health_ok, "backend": "fake", "version": "0.0.0-fake",
                "models": ["fake-model", "qwen3:8b"]}

    def list_models(self):
        return ["fake-model", "qwen3:8b"]

    def chat(self, messages, stream=True, model=None):
        if not stream:
            yield {"delta": "".join(self.tokens)}
            return
        for token in self.tokens:
            yield {"delta": token}


class FakeIndex:
    """Deterministic stand-in for steward.retrieval.RetrievalIndex."""

    def __init__(self, hits=None, stats=None, summary=None):
        self.hits = list(hits) if hits else []
        self.stats_value = dict(stats) if stats else {
            "documents": 0, "chunks": 0, "vector_dim": 0,
            "db_size_bytes": 0, "indexed_at": None, "db_path": "/tmp/fake-index.db",
        }
        self.summary_value = dict(summary) if summary else {
            "indexed": 1, "reindexed": 0, "skipped": 0, "failed": 0,
            "seconds": 0.01, "documents": 1, "chunks": 3,
        }
        self.index_calls = 0
        self.search_queries = []

    def stats(self):
        return dict(self.stats_value)

    def search(self, query, k=8):
        self.search_queries.append(query)
        return list(self.hits)[:k]

    def index_paths(self, paths=None, progress=None):
        self.index_calls += 1
        return dict(self.summary_value)


@pytest.fixture
def env(tmp_path):
    return {
        "tmp": tmp_path,
        "db_path": tmp_path / "db" / "steward.db",
        "secret_file": tmp_path / "db" / "pairing.key",
    }


@pytest.fixture
def start_daemon(env):
    """Factory fixture: start a server on port 0, tear all of them down."""
    started = []

    def start(runtime, advertise=False, db_path=None, secret_file=None, **kw):
        srv = daemon_mod.create_server(
            ("127.0.0.1", 0), runtime=runtime,
            db_path=Path(db_path) if db_path else env["db_path"],
            secret_file=Path(secret_file) if secret_file else env["secret_file"],
            advertise=advertise, **kw)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        started.append((srv, thread))
        return srv, srv.server_address[1]

    yield start
    for srv, thread in started:
        srv.shutdown_server()
        thread.join(timeout=5)


@pytest.fixture(autouse=True)
def reset_index_override():
    yield
    webapp_serve.set_index_override(None)


def request(port, method, path, body=None, token=None, headers=None, raw=False):
    """JSON helper over http.client; raw=True returns the text body untouched."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    hdrs = {"Content-Type": "application/json"}
    if token:
        hdrs["Authorization"] = "Bearer " + token
    if headers:
        hdrs.update(headers)
    payload = json.dumps(body) if body is not None else None
    conn.request(method, path, body=payload, headers=hdrs)
    resp = conn.getresponse()
    data = resp.read().decode("utf-8", "replace")
    conn.close()
    if raw:
        return resp.status, resp.getheader("Content-Type"), data
    try:
        parsed = json.loads(data) if data else {}
    except json.JSONDecodeError:
        parsed = data
    return resp.status, parsed


def pair(port):
    status, code_resp = request(port, "GET", "/pair/code")
    assert status == 200, code_resp
    status, resp = request(port, "POST", "/v1/pair",
                           {"code": code_resp["code"], "device": "webapp-pytest"})
    assert status == 200, resp
    return resp["token"]


# -- static asset routes --------------------------------------------------------


def test_webapp_static_routes_serve_assets(start_daemon):
    srv, port = start_daemon(FakeRuntime())

    status, ctype, body = request(port, "GET", "/", raw=True)
    assert status == 200
    assert ctype.startswith("text/html")
    assert "<title>Steward</title>" in body
    assert "/style.css" in body and "/app.js" in body

    status, ctype, body = request(port, "GET", "/index.html", raw=True)
    assert status == 200 and ctype.startswith("text/html")
    assert "Steward" in body

    status, ctype, body = request(port, "GET", "/app.js", raw=True)
    assert status == 200
    assert ctype.startswith("application/javascript")
    assert "renderMarkdown" in body

    status, ctype, body = request(port, "GET", "/style.css", raw=True)
    assert status == 200
    assert ctype.startswith("text/css")
    assert ":root" in body


def test_webapp_static_served_without_auth(start_daemon):
    # The client must be reachable before pairing; auth gates data, not the shell.
    srv, port = start_daemon(FakeRuntime())
    status, ctype, body = request(port, "GET", "/", raw=True)
    assert status == 200
    assert "Steward" in body


def test_no_external_asset_urls(start_daemon):
    # Zero external network requests: no http(s) reference anywhere in the
    # built files. Everything is same-origin, system fonts, data-free icons.
    for name in ("index.html", "app.js", "style.css"):
        content = (WEBAPP_DIR / name).read_text(encoding="utf-8")
        assert "http://" not in content, name + " references http://"
        assert "https://" not in content, name + " references https://"


# -- client renderer (Node harness over the real app.js) ------------------------


def node_available():
    return shutil.which("node") or shutil.which("nodejs")


def run_node(script):
    node = node_available()
    if not node:
        pytest.skip("node not installed on this host")
    proc = subprocess.run([node, "-e", script], capture_output=True, text=True,
                          timeout=30)
    assert proc.returncode == 0, "node harness failed:\n%s" % proc.stderr
    return proc.stdout


def node_harness(body_lines):
    app = (WEBAPP_DIR / "app.js").read_text(encoding="utf-8")
    return ("const appjs = " + json.dumps(app) + ";\n"
            "eval(appjs);\n" + "\n".join(body_lines))


def test_markdown_rendering_escapes_script(start_daemon):
    out = run_node(node_harness([
        "const S = globalThis.Steward;",
        "function must(cond, msg){ if(!cond){ throw new Error(msg); } }",
        "const rendered = S.renderMarkdown('hello <script>alert(1)</script> world');",
        "must(rendered.indexOf('<script>') === -1, 'raw script tag leaked into output');",
        "must(rendered.indexOf('</script>') === -1, 'raw closing script tag leaked');",
        "must(rendered.indexOf('&lt;script&gt;') !== -1, 'script tag was not escaped');",
        "const img = S.renderMarkdown('x <img src=x onerror=alert(1)> y');",
        "must(img.indexOf('<img') === -1, 'raw img tag leaked');",
        "must(img.indexOf('&lt;img') !== -1, 'img tag was not escaped');",
        "console.log('ESCAPE_OK');",
    ]))
    assert "ESCAPE_OK" in out


def test_markdown_rendering_keeps_real_markdown(start_daemon):
    out = run_node(node_harness([
        "const S = globalThis.Steward;",
        "function must(cond, msg){ if(!cond){ throw new Error(msg); } }",
        "let md = S.renderMarkdown('**bold** and *italic* and `code`');",
        "must(md.indexOf('<strong>bold</strong>') !== -1, 'bold not rendered');",
        "must(md.indexOf('<em>italic</em>') !== -1, 'italic not rendered');",
        "must(md.indexOf('<code>code</code>') !== -1, 'inline code not rendered');",
        "md = S.renderMarkdown('- one\\n- two');",
        "must(md.indexOf('<ul>') !== -1 && md.indexOf('<li>one</li>') !== -1, 'list not rendered');",
        "md = S.renderMarkdown('See [link](https://example.com/a) here');",
        "must(md.indexOf('<a href=\"https://example.com/a\">link</a>') !== -1, 'link not rendered');",
        "md = S.renderMarkdown('bad [x](javascript:alert(1))');",
        "must(md.indexOf('<a') === -1, 'javascript: link must be dropped');",
        "must(md.indexOf('javascript:') === -1, 'javascript: scheme leaked');",
        "md = S.renderMarkdown('# Head\\n\\nText here.');",
        "must(md.indexOf('<h3>Head</h3>') !== -1 && md.indexOf('<p>Text here.</p>') !== -1, 'heading/paragraph not rendered');",
        "console.log('MD_OK');",
    ]))
    assert "MD_OK" in out


def test_qr_matrix_encoder_shapes(start_daemon):
    out = run_node(node_harness([
        "const S = globalThis.Steward;",
        "function must(cond, msg){ if(!cond){ throw new Error(msg); } }",
        "const url = 'steward://pair?host=github.com&port=3877';",
        "const qr = S.qrMatrixEncode(url);",
        "must(qr && qr.size > 0, 'no qr produced');",
        "must(qr.matrix.length === qr.size, 'matrix rows mismatch size');",
        "let dark = 0;",
        "for (let y = 0; y < qr.size; y++) for (let x = 0; x < qr.size; x++) dark += qr.matrix[y][x];",
        "must(dark > qr.size, 'matrix is not populated');",
        "must(qr.size >= 25, 'version too small');",
        "console.log('QR_OK ' + qr.size);",
    ]))
    assert "QR_OK" in out


# -- webapp API auth -------------------------------------------------------------


def test_webapp_api_requires_auth(start_daemon):
    srv, port = start_daemon(FakeRuntime())
    status, resp = request(port, "GET", "/v1/webapp/info")
    assert status == 401
    status, resp = request(port, "GET", "/v1/files")
    assert status == 401
    status, resp = request(port, "POST", "/v1/files/search", body={"query": "x"})
    assert status == 401

    token = pair(port)
    status, resp = request(port, "GET", "/v1/webapp/info", token=token)
    assert status == 200
    status, resp = request(port, "GET", "/v1/files", token=token)
    assert status == 200


def test_webapp_settings_info(start_daemon, env):
    srv, port = start_daemon(FakeRuntime())
    token = pair(port)
    status, info = request(port, "GET", "/v1/webapp/info", token=token)
    assert status == 200
    assert info["model"] == "qwen3:8b"
    assert info["backend_mode"] == "fake-runtime"
    assert info["data_dir"] == str(env["tmp"])
    assert info["license"]["state"] == "free"
    assert info["license"]["subscription"] is False
    assert info["telemetry"] is False
    assert info["pairing_url"].startswith("steward://pair?host=")
    assert info["version"] == daemon_mod.VERSION
    assert info["device"] == "webapp-pytest"


def test_webapp_settings_info_after_restart_keeps_working(start_daemon, env):
    # The settings route derives its data root from the daemon's db path, so
    # folders added in one run persist for the next run on the same home.
    (env["tmp"] / "notes").mkdir()
    rt_a = FakeRuntime()
    srv_a, port_a = start_daemon(rt_a)

    webapp_serve.set_index_override(FakeIndex())
    token = pair(port_a)
    status, resp = request(port_a, "POST", "/v1/files/folders", body={"path": str(env["tmp"] / "notes")}, token=token)
    assert status == 201
    assert str(env["tmp"] / "notes") in resp["folders"]
    srv_a.shutdown_server()

    rt_b = FakeRuntime()
    srv_b, port_b = start_daemon(rt_b)
    token_b = pair(port_b)
    # A fresh daemon on the same home reads the same config.json.
    status, resp = request(port_b, "GET", "/v1/files", token=token_b)
    assert status == 200
    assert str(env["tmp"] / "notes") in resp["folders"]


def test_webapp_unknown_path_still_404(start_daemon):
    srv, port = start_daemon(FakeRuntime())
    status, resp = request(port, "GET", "/does-not-exist")
    assert status == 404
    # The mount did not shadow the daemon's own routes.
    token = pair(port)
    status, resp = request(port, "GET", "/v1/conversations", token=token)
    assert status == 200
    status, raw_resp, _ = request(port, "POST", "/v1/chat", body={"text": "hi"}, token=token, raw=True)
    assert status == 200


# -- files: folders, index, search ------------------------------------------------


def test_webapp_files_folders_crud(start_daemon, env):
    srv, port = start_daemon(FakeRuntime())
    webapp_serve.set_index_override(FakeIndex())
    token = pair(port)

    status, resp = request(port, "GET", "/v1/files", token=token)
    assert status == 200 and resp["folders"] == []

    missing = env["tmp"] / "missing"
    status, resp = request(port, "POST", "/v1/files/folders", body={"path": str(missing)}, token=token)
    assert status == 400  # a real directory is required

    (env["tmp"] / "notes").mkdir()
    status, resp = request(port, "POST", "/v1/files/folders", body={"path": str(env["tmp"] / "notes")}, token=token)
    assert status == 201
    assert str(env["tmp"] / "notes") in resp["folders"]

    # adding the same folder again is idempotent
    status, resp = request(port, "POST", "/v1/files/folders", body={"path": str(env["tmp"] / "notes")}, token=token)
    assert status == 201 and resp["folders"].count(str(env["tmp"] / "notes")) == 1

    status, resp = request(port, "GET", "/v1/files", token=token)
    assert status == 200 and resp["folders"] == [str(env["tmp"] / "notes")]

    status, resp = request(port, "DELETE", "/v1/files/folders?path=" + str(env["tmp"] / "notes"), token=token)
    assert status == 200 and resp["folders"] == []


def test_webapp_files_index_build_requires_folder(start_daemon):
    srv, port = start_daemon(FakeRuntime())
    fake = FakeIndex()
    webapp_serve.set_index_override(fake)
    token = pair(port)

    status, resp = request(port, "POST", "/v1/files/index", body={}, token=token)
    assert status == 400  # nothing configured yet
    assert fake.index_calls == 0


def test_webapp_files_search_with_citations(start_daemon):
    srv, port = start_daemon(FakeRuntime())
    fake = FakeIndex(hits=[
        {"path": "/tmp/notes/hermes.md", "ordinal": 0, "offset": 12,
         "score": 0.71, "snippet": "Hermes cron scheduling notes",
         "text": "Hermes cron scheduling notes", "chunk_id": "abc123"},
        {"path": "/tmp/notes/serving.md", "ordinal": 1, "offset": 4,
         "score": 0.62, "snippet": "Serving stack on GB10",
         "text": "Serving stack on GB10", "chunk_id": "def456"},
    ], stats={"documents": 2, "chunks": 5, "vector_dim": 768,
              "db_size_bytes": 4096, "indexed_at": 12345, "db_path": "/tmp/x.db"})
    webapp_serve.set_index_override(fake)
    token = pair(port)

    status, resp = request(port, "POST", "/v1/files/search", body={"query": "hermes", "k": 3}, token=token)
    assert status == 200
    assert resp["query"] == "hermes"
    assert len(resp["hits"]) == 2
    first = resp["hits"][0]
    assert first["path"] == "/tmp/notes/hermes.md"
    assert first["score"] == 0.71
    assert "offset" in first and "snippet" in first
    assert fake.search_queries == ["hermes"]

    # empty query is a 400, badly shaped k falls back to 8
    status, resp = request(port, "POST", "/v1/files/search", body={"query": "  "}, token=token)
    assert status == 400

    status, resp = request(port, "GET", "/v1/files", token=token)
    assert status == 200
    assert resp["index"]["documents"] == 2
    assert resp["index"]["vector_dim"] == 768


def test_webapp_files_search_empty_index(start_daemon):
    srv, port = start_daemon(FakeRuntime())
    webapp_serve.set_index_override(FakeIndex())  # no hits
    token = pair(port)
    status, resp = request(port, "POST", "/v1/files/search", body={"query": "nothing"}, token=token)
    assert status == 200 and resp["hits"] == []


# -- client update logic (node harness over the real app.js) -------------------


def run_node_async(script):
    """Run a Node script with top-level await support (--input-type=module).
    The script should write its result to stdout; we assert the exit code."""
    node = node_available()
    if not node:
        pytest.skip("node not installed on this host")
    proc = subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, timeout=40)
    assert proc.returncode == 0, "node harness failed:\n%s" % proc.stderr
    return proc.stdout


def update_harness(body):
    """Load app.js with a fake localStorage/fetch/Response so the update
    module's autocheck + check paths run without a daemon or a DOM."""
    app = (WEBAPP_DIR / "app.js").read_text(encoding="utf-8")
    fixture = json.dumps(UPDATE_FIXTURE)
    return (
        "globalThis.localStorage = (() => { const m = new Map(); return {\n"
        "  getItem: k => (m.has(k) ? m.get(k) : null),\n"
        "  setItem: (k, v) => m.set(k, v),\n"
        "  removeItem: k => m.delete(k),\n"
        "}; })();\n"
        "const UPDATE_FIXTURE = " + fixture + ";\n"
        "let fetchCalls = [];\n"
        "globalThis.fetch = async (url, opts) => {\n"
        "  fetchCalls.push(String(url));\n"
        "  if (String(url).endsWith('/v1/update/check')) {\n"
        "    return new Response(JSON.stringify(UPDATE_FIXTURE), { status: 200 });\n"
        "  }\n"
        "  return new Response('{}', { status: 200 });\n"
        "};\n"
        "const appjs = " + json.dumps(app) + ";\n"
        "eval(appjs);\n"
        + "\n".join(body)
    )


UPDATE_FIXTURE = {
    "ok": True,
    "state": "ok",
    "available": True,
    "catalog": {"available": False, "current_version": "embedded",
                "latest_version": "2026-10-28.1", "model_count": 40,
                "released": "2026-10-28"},
    "app": {"available": True, "current_version": "1.0.1",
            "latest_version": "1.1.0", "released": "2026-10-28",
            "notes": "Faster first token", "notes_url": None,
            "min_source_version": "1.0.0", "payload_for_platform": True},
    "source": "http://127.0.0.1:9/nope.json",
    "checked_at": "2026-10-28T19:00:00Z",
    "manifest": {"version": "2026-10-28.1"},
}


def test_client_autocheck_off_makes_no_request():
    # The acceptance: auto-check off means the client makes NO check request.
    # With the pref stored off and a valid session, maybeCheckForUpdates must
    # not call /v1/update/check.
    out = run_node_async(update_harness([
        "const S = globalThis.Steward;",
        "S.setUpdateAutocheck(false);",
        "S._test.setSessionToken('tok');",
        "await S.maybeCheckForUpdates(false);",
        "if (fetchCalls.some(u => u.includes('/v1/update/check'))) throw new Error('check requested while auto-check off');",
        "if (S.updateAutocheckEnabled()) throw new Error('toggle did not stick');",
        "console.log('NOCHECK_OK');",
    ]))
    assert "NOCHECK_OK" in out


def test_client_autocheck_on_requests_and_flags_available():
    # Default is on: a load-time auto-check fires /v1/update/check and the
    # module records the newer app version as available.
    out = run_node_async(update_harness([
        "const S = globalThis.Steward;",
        "S._test.setSessionToken('tok');",
        "if (!S.updateAutocheckEnabled()) throw new Error('default should be on');",
        "await S.maybeCheckForUpdates(false);",
        "if (!fetchCalls.some(u => u.includes('/v1/update/check'))) throw new Error('auto-check did not fire');",
        "const st = S._test.getUpdateState();",
        "if (!st.available) throw new Error('newer app not flagged available');",
        "if ((st.last.app.latest_version) !== '1.1.0') throw new Error('latest version not recorded');",
        "console.log('CHECK_OK');",
    ]))
    assert "CHECK_OK" in out


def test_client_check_forced_even_when_autocheck_off():
    # The Settings "Check for updates" button always works regardless of the
    # auto-check toggle (force bypasses the pref).
    out = run_node_async(update_harness([
        "const S = globalThis.Steward;",
        "S.setUpdateAutocheck(false);",
        "S._test.setSessionToken('tok');",
        "await S.maybeCheckForUpdates(true);",
        "if (!fetchCalls.some(u => u.includes('/v1/update/check'))) throw new Error('forced check did not fire');",
        "console.log('FORCE_OK');",
    ]))
    assert "FORCE_OK" in out
