/* Steward webapp client.
 * No build step, no CDN, no external fonts, no network beyond the daemon.
 * Screens: chat (streaming), files, brief, settings, pair.
 * Auth: a paired token lives in memory and localStorage; every mutating
 * fetch sends it as a Bearer header. A 401 drops back to the Pair screen.
 *
 * Pure helpers (escapeHtml, renderMarkdown, qrMatrix) are deliberately free
 * of DOM dependencies so tests can run them in Node.
 */
(function () {
  'use strict';

  var NS = (typeof globalThis !== 'undefined') ? globalThis : this;
  var SESSION_KEY = 'steward.session.v1';

  /* ------------------------------------------------------------------ */
  /* escaping and markdown-lite rendering                                */
  /* ------------------------------------------------------------------ */

  function escapeHtml(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function sanitizeUrl(url) {
    var u = String(url || '').trim();
    // Only plain web, mail and local targets. A leading '/' or '#' stays local.
    if (/^(https?|ftp):\/\//i.test(u) || /^mailto:/i.test(u) || /^#/.test(u) || /^\//.test(u)) {
      return u;
    }
    return null;
  }

  function inlineMarkup(text) {
    // Escape first, then apply tokens to the escaped string, so any
    // <script> or attribute in the source can never execute.
    var s = escapeHtml(text);
    var stash = [];
    s = s.replace(/`([^`]+)`/g, function (_, code) {
      stash.push('<code>' + code + '</code>');
      return '\u0000' + (stash.length - 1) + '\u0000';
    });
    s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/\*([^*\n]+)\*/g, function (m, p) {
      if (/^\s|\s$/.test(p)) { return m; }
      return '<em>' + p + '</em>';
    });
    s = s.replace(/\[([^\]]+)\]\(([^)]+)\)/g, function (_, label, url) {
      var safe = sanitizeUrl(url);
      return safe ? '<a href="' + escapeHtml(safe) + '">' + label + '</a>' : label;
    });
    s = s.replace(/\u0000(\d+)\u0000/g, function (_, n) { return stash[+n] || ''; });
    return s;
  }

  function renderMarkdown(source) {
    var src = String(source == null ? '' : source);
    var lines = src.split('\n');
    var out = [];
    var i = 0;
    var inCode = false;
    var codeBuf = [];
    var listType = null;

    function closeList() {
      if (listType) { out.push('</' + listType + '>'); listType = null; }
    }
    function openList(t) {
      if (listType !== t) { closeList(); out.push('<' + t + '>'); listType = t; }
    }

    while (i < lines.length) {
      var raw = lines[i];
      var trimmed = raw.trim();

      if (/^```/.test(trimmed)) {
        closeList();
        if (!inCode) {
          inCode = true; codeBuf = [];
        } else {
          inCode = false;
          out.push('<pre><code>' + codeBuf.join('\n') + '</code></pre>');
        }
        i += 1;
        continue;
      }
      if (inCode) {
        codeBuf.push(escapeHtml(raw));
        i += 1;
        continue;
      }
      if (trimmed === '') { closeList(); i += 1; continue; }

      var heading = /^(#{1,6})\s+(.*)$/.exec(trimmed);
      if (heading) {
        closeList();
        var level = Math.min(heading[1].length + 2, 4);
        out.push('<h' + level + '>' + inlineMarkup(heading[2]) + '</h' + level + '>');
        i += 1;
        continue;
      }
      if (/^(-{3,}|\*{3,})\s*$/.test(trimmed)) {
        closeList();
        out.push('<hr>');
        i += 1;
        continue;
      }
      var bullet = /^[-*]\s+(.*)$/.exec(trimmed);
      if (bullet) {
        openList('ul');
        out.push('<li>' + inlineMarkup(bullet[1]) + '</li>');
        i += 1;
        continue;
      }
      var numbered = /^\d+[.)]\s+(.*)$/.exec(trimmed);
      if (numbered) {
        openList('ol');
        out.push('<li>' + inlineMarkup(numbered[1]) + '</li>');
        i += 1;
        continue;
      }

      // plain paragraph: keep collecting until a blank or structural line
      closeList();
      var paragraph = [];
      while (i < lines.length) {
        var p = lines[i].trim();
        if (p === '' || /^```/.test(lines[i].trim()) || /^#{1,6}\s/.test(lines[i].trim()) ||
            /^[-*]\s+/.test(lines[i].trim()) || /^\d+[.)]\s+/.test(lines[i].trim())) {
          break;
        }
        paragraph.push(p);
        i += 1;
      }
      if (paragraph.length) {
        out.push('<p>' + inlineMarkup(paragraph.join(' ')) + '</p>');
      }
    }
    if (inCode) {
      out.push('<pre><code>' + codeBuf.join('\n') + '</code></pre>');
    }
    closeList();
    return out.join('\n');
  }

  /* ------------------------------------------------------------------ */
  /* QR encoder (byte mode, error correction level M, versions 1 to 4)   */
  /* ------------------------------------------------------------------ */

  var QR_EXP = new Uint8Array(512);
  var QR_LOG = new Uint8Array(256);
  (function () {
    var x = 1;
    for (var i = 0; i < 255; i++) {
      QR_EXP[i] = x;
      QR_LOG[x] = i;
      x <<= 1;
      if (x & 0x100) { x ^= 0x11D; }
    }
    for (var j = 255; j < 512; j++) { QR_EXP[j] = QR_EXP[j - 255]; }
  })();

  function qrMul(a, b) {
    return (a && b) ? QR_EXP[QR_LOG[a] + QR_LOG[b]] : 0;
  }

  function rsComputeDivisor(degree) {
    var result = [];
    for (var i = 0; i < degree - 1; i++) { result.push(0); }
    result.push(1);
    var root = 1;
    for (var j = 0; j < degree; j++) {
      for (var k = 0; k < result.length; k++) {
        result[k] = qrMul(result[k], root);
        if (k + 1 < result.length) { result[k] ^= result[k + 1]; }
      }
      root = qrMul(root, 0x02);
    }
    return result;
  }

  function rsComputeRemainder(data, divisor) {
    var result = divisor.map(function () { return 0; });
    for (var i = 0; i < data.length; i++) {
      var factor = data[i] ^ result.shift();
      result.push(0);
      for (var j = 0; j < result.length; j++) {
        result[j] ^= qrMul(divisor[j], factor);
      }
    }
    return result;
  }

  // Level M block tables for versions 1 to 4 (ISO 18004 Table 7).
  var QR_VERSIONS = [
    null,
    { total: 26, ec: 10, blockData: [16], align: null, remainder: 0, cap: 14 },
    { total: 44, ec: 16, blockData: [28], align: [18], remainder: 7, cap: 26 },
    { total: 70, ec: 26, blockData: [44], align: [22], remainder: 7, cap: 42 },
    { total: 100, ec: 18, blockData: [32, 32], align: [26], remainder: 7, cap: 62 }
  ];

  function pushBits(bits, value, count) {
    for (var i = count - 1; i >= 0; i--) { bits.push((value >> i) & 1); }
  }

  function utf8Encode(str) {
    if (typeof TextEncoder !== 'undefined') {
      return Array.prototype.slice.call(new TextEncoder().encode(str));
    }
    var out = [];
    for (var i = 0; i < str.length; i++) {
      var code = str.codePointAt(i);
      if (code > 0xFFFF) { i += 1; }
      if (code < 0x80) { out.push(code); }
      else if (code < 0x800) { out.push(0xC0 | (code >> 6), 0x80 | (code & 0x3F)); }
      else if (code < 0x10000) { out.push(0xE0 | (code >> 12), 0x80 | ((code >> 6) & 0x3F), 0x80 | (code & 0x3F)); }
      else { out.push(0xF0 | (code >> 18), 0x80 | ((code >> 12) & 0x3F), 0x80 | ((code >> 6) & 0x3F), 0x80 | (code & 0x3F)); }
    }
    return out;
  }

  function markFunc(func, size, x, y) {
    if (x >= 0 && x < size && y >= 0 && y < size) { func[y][x] = 1; }
  }

  function drawFunctionPatterns(modules, func, size, align) {
    function set(x, y, dark) {
      func[y][x] = 1;
      modules[y][x] = dark ? 1 : 0;
    }
    // finder patterns with separators
    [[0, 0], [size - 7, 0], [0, size - 7]].forEach(function (pos) {
      for (var r = -1; r <= 7; r++) {
        for (var c = -1; c <= 7; c++) {
          var x = pos[0] + c;
          var y = pos[1] + r;
          if (x < 0 || x >= size || y < 0 || y >= size) { continue; }
          var inside = c >= 0 && c <= 6 && r >= 0 && r <= 6;
          var dark = inside && (c === 0 || c === 6 || r === 0 || r === 6 ||
            (c >= 2 && c <= 4 && r >= 2 && r <= 4));
          set(x, y, dark);
        }
      }
    });
    // timing patterns
    for (var t = 8; t < size - 8; t++) {
      set(t, 6, t % 2 === 0);
      set(6, t, t % 2 === 0);
    }
    // alignment patterns
    if (align) {
      align.forEach(function (cy) {
        align.forEach(function (cx) {
          if (func[cy][cx]) { return; }
          for (var r = -2; r <= 2; r++) {
            for (var c = -2; c <= 2; c++) {
              var x = cx + c;
              var y = cy + r;
              if (x < 0 || x >= size || y < 0 || y >= size) { continue; }
              var dark = Math.max(Math.abs(r), Math.abs(c)) !== 1;
              set(x, y, dark);
            }
          }
        });
      });
    }
    // format info cells (both copies), reserved so data never lands on them
    var i;
    for (i = 0; i <= 5; i++) { markFunc(func, size, 8, i); }
    markFunc(func, size, 8, 7);
    markFunc(func, size, 8, 8);
    markFunc(func, size, 7, 8);
    for (i = 9; i < 15; i++) { markFunc(func, size, 14 - i, 8); }
    for (i = 0; i < 8; i++) { markFunc(func, size, size - 1 - i, 8); }
    for (i = 8; i < 15; i++) { markFunc(func, size, 8, size - 15 + i); }
    // dark module
    set(8, size - 8, true);
  }

  function qrFormatBits(mask) {
    var data = mask; // EC level M contributed zero high bits
    var rem = data;
    for (var i = 0; i < 10; i++) {
      rem = (rem << 1) ^ ((rem >>> 9) * 0x537);
    }
    return ((data << 10) | rem) ^ 0x5412;
  }

  function maskBit(mask, x, y) {
    switch (mask) {
      case 0: return (x + y) % 2 === 0;
      case 1: return y % 2 === 0;
      case 2: return x % 3 === 0;
      case 3: return (x + y) % 3 === 0;
      case 4: return (Math.floor(y / 2) + Math.floor(x / 3)) % 2 === 0;
      case 5: return (x * y) % 2 + (x * y) % 3 === 0;
      case 6: return ((x * y) % 2 + (x * y) % 3) % 2 === 0;
      default: return ((x + y) % 2 + (x * y) % 3) % 2 === 0;
    }
  }

  function drawFormatBits(modules, bits, size) {
    function set(x, y, v) { modules[y][x] = v; }
    var i;
    for (i = 0; i <= 5; i++) { set(8, i, (bits >> i) & 1); }
    set(8, 7, (bits >> 6) & 1);
    set(8, 8, (bits >> 7) & 1);
    set(7, 8, (bits >> 8) & 1);
    for (i = 9; i < 15; i++) { set(14 - i, 8, (bits >> i) & 1); }
    for (i = 0; i < 8; i++) { set(size - 1 - i, 8, (bits >> i) & 1); }
    for (i = 8; i < 15; i++) { set(8, size - 15 + i, (bits >> i) & 1); }
    set(8, size - 8, 1);
  }

  function maskAndPenalize(base, func, mask, size) {
    var copy = base.map(function (row) { return new Uint8Array(row); });
    for (var y = 0; y < size; y++) {
      for (var x = 0; x < size; x++) {
        if (!func[y][x] && maskBit(mask, x, y)) { copy[y][x] ^= 1; }
      }
    }
    drawFormatBits(copy, qrFormatBits(mask), size);
    return copy;
  }

  function penaltyScore(grid, size) {
    var score = 0;
    // rule 1: runs of 5 or more of one color
    function runs(vertical) {
      var s = 0;
      for (var i = 0; i < size; i++) {
        var run = 1;
        var prev = vertical ? grid[0][i] : grid[i][0];
        for (var j = 1; j < size; j++) {
          var c = vertical ? grid[j][i] : grid[i][j];
          if (c === prev) { run += 1; }
          else { if (run >= 5) { s += run - 2; } run = 1; prev = c; }
        }
        if (run >= 5) { s += run - 2; }
      }
      return s;
    }
    score += runs(false) + runs(true);
    // rule 2: 2x2 blocks of one color
    for (var y = 0; y < size - 1; y++) {
      for (var x = 0; x < size - 1; x++) {
        var c = grid[y][x];
        if (grid[y][x + 1] === c && grid[y + 1][x] === c && grid[y + 1][x + 1] === c) { score += 3; }
      }
    }
    // rule 3: finder-like 1:1:3:1:1 with light borders (approximate)
    var pattern = [0, 0, 0, 0, 1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0];
    function finderLike(horiz, y, x) {
      for (var i = 0; i < 15; i++) {
        var c = horiz ? grid[y][x + i] : grid[x + i][y];
        if (c !== pattern[i]) { return false; }
      }
      return true;
    }
    for (var y2 = 0; y2 < size; y2++) {
      for (var x2 = 0; x2 < size; x2++) {
        if (x2 + 15 <= size && finderLike(true, y2, x2)) { score += 40; }
        if (x2 + 15 <= size && finderLike(false, y2, x2)) { score += 40; }
      }
    }
    // rule 4: dark module proportion
    var dark = 0;
    for (var y3 = 0; y3 < size; y3++) {
      for (var x3 = 0; x3 < size; x3++) { if (grid[y3][x3]) { dark += 1; } }
    }
    score += Math.floor(Math.abs(dark / (size * size) * 100 - 50) / 5) * 10;
    return score;
  }

  function qrMatrixEncode(text) {
    var bytes = utf8Encode(String(text == null ? '' : text));
    if (bytes.length === 0) { return null; }
    var version = 0;
    for (var v = 1; v <= 4; v++) {
      if (bytes.length <= QR_VERSIONS[v].cap) { version = v; break; }
    }
    if (!version) { return null; }
    var cfg = QR_VERSIONS[version];
    var size = version * 4 + 17;
    var modules = [];
    var func = [];
    for (var i = 0; i < size; i++) {
      modules.push(new Uint8Array(size));
      func.push(new Uint8Array(size));
    }
    drawFunctionPatterns(modules, func, size, cfg.align);

    // assemble the data bit stream
    var bits = [];
    pushBits(bits, 4, 4); // byte mode
    pushBits(bits, bytes.length, 8);
    for (var b = 0; b < bytes.length; b++) { pushBits(bits, bytes[b], 8); }
    var capacityBits = cfg.total * 8;
    if (bits.length < capacityBits) {
      pushBits(bits, 0, Math.min(4, capacityBits - bits.length));
      while (bits.length % 8 !== 0) { bits.push(0); }
      var pad = [0xEC, 0x11];
      var pi = 0;
      while (bits.length < capacityBits) {
        pushBits(bits, pad[pi % 2], 8);
        pi += 1;
      }
    }
    var dataCodewords = [];
    for (var d = 0; d < bits.length; d += 8) {
      var byte = 0;
      for (var k = 0; k < 8; k++) { byte = (byte << 1) | bits[d + k]; }
      dataCodewords.push(byte);
    }

    // split into blocks, RS each, interleave
    var divisor = rsComputeDivisor(cfg.ec);
    var blockData = [];
    var blockEc = [];
    var offset = 0;
    for (var blk = 0; blk < cfg.blockData.length; blk++) {
      var sizeData = cfg.blockData[blk];
      var block = dataCodewords.slice(offset, offset + sizeData);
      offset += sizeData;
      blockData.push(block);
      blockEc.push(rsComputeRemainder(block, divisor));
    }
    var maxData = 0;
    blockData.forEach(function (b) { maxData = Math.max(maxData, b.length); });
    var result = [];
    for (var m = 0; m < maxData; m++) {
      for (var bi = 0; bi < blockData.length; bi++) {
        if (m < blockData[bi].length) { result.push(blockData[bi][m]); }
      }
    }
    for (var e = 0; e < cfg.ec; e++) {
      for (var be = 0; be < blockEc.length; be++) {
        result.push(blockEc[be][e]);
      }
    }

    var dataBits = [];
    for (var r = 0; r < result.length; r++) { pushBits(dataBits, result[r], 8); }
    for (var rr = 0; rr < cfg.remainder; rr++) { dataBits.push(0); }

    // place data, skipping function modules
    var bitIndex = 0;
    var upward = true;
    for (var right = size - 1; right >= 1; right -= 2) {
      if (right === 6) { right = 5; } // vertical timing column
      for (var vert = 0; vert < size; vert++) {
        var y = upward ? (size - 1 - vert) : vert;
        for (var col = 0; col < 2; col++) {
          var x = right - col;
          if (x < 0) { continue; }
          if (func[y][x]) { continue; }
          if (bitIndex < dataBits.length) {
            if (dataBits[bitIndex]) { modules[y][x] = 1; }
            bitIndex += 1;
          }
        }
      }
      upward = !upward;
    }

    // pick the least-penalty mask
    var best = null;
    var bestPenalty = Infinity;
    for (var mask = 0; mask < 8; mask++) {
      var candidate = maskAndPenalize(modules, func, mask, size);
      var p = penaltyScore(candidate, size);
      if (p < bestPenalty) { bestPenalty = p; best = candidate; }
    }
    return { size: size, matrix: best };
  }

  function drawQr(canvas, text) {
    var qr = qrMatrixEncode(text);
    if (!qr) { return false; }
    var size = qr.size;
    var matrix = qr.matrix;
    var scale = 4;
    var pad = 4;
    var dim = size * scale + pad * 2;
    if (canvas.width !== dim) { canvas.width = dim; }
    if (canvas.height !== dim) { canvas.height = dim; }
    var ctx = canvas.getContext('2d');
    ctx.fillStyle = '#ffffff';
    ctx.fillRect(0, 0, dim, dim);
    ctx.fillStyle = '#000000';
    for (var y = 0; y < size; y++) {
      for (var x = 0; x < size; x++) {
        if (matrix[y][x]) { ctx.fillRect(pad + x * scale, pad + y * scale, scale, scale); }
      }
    }
    return true;
  }

  /* ------------------------------------------------------------------ */
  /* session and API                                                     */
  /* ------------------------------------------------------------------ */

  function loadSession() {
    try {
      var raw = localStorage.getItem(SESSION_KEY);
      if (raw) {
        var parsed = JSON.parse(raw);
        if (parsed && parsed.token) { return parsed; }
      }
    } catch (e) { /* private mode etc: fall through to empty */ }
    return null;
  }
  function saveSession(s) {
    try { localStorage.setItem(SESSION_KEY, JSON.stringify(s)); } catch (e) { /* memory only */ }
  }
  function clearSession() {
    try { localStorage.removeItem(SESSION_KEY); } catch (e) { /* ignore */ }
  }

  var session = loadSession() || { token: '', device: 'Phone' };

  function handleUnauthorized() {
    session.token = '';
    clearSession();
    var err = new Error('unauthorized');
    err.status = 401;
    showScreen('pair');
    return Promise.reject(err);
  }

  function api(path, opts) {
    opts = opts || {};
    var headers = { 'Content-Type': 'application/json' };
    if (opts.headers) { Object.keys(opts.headers).forEach(function (k) { headers[k] = opts.headers[k]; }); }
    if (session.token) { headers['Authorization'] = 'Bearer ' + session.token; }
    return fetch(path, {
      method: opts.method || 'GET',
      headers: headers,
      body: opts.body ? JSON.stringify(opts.body) : undefined
    }).then(function (resp) {
      if (resp.status === 401) {
        return handleUnauthorized();
      }
      if (!resp.ok) {
        return resp.text().then(function (text) {
          var payload = null;
          try { payload = JSON.parse(text); } catch (e) { /* body was not JSON */ }
          var err = new Error((payload && payload.error) || ('request failed (' + resp.status + ')'));
          err.status = resp.status;
          throw err;
        });
      }
      return resp;
    });
  }

  function apiJson(path, opts) {
    return api(path, opts).then(function (resp) { return resp.json(); });
  }

  /* ------------------------------------------------------------------ */
  /* helpers                                                             */
  /* ------------------------------------------------------------------ */

  function el(tag, className, html) {
    var node = document.createElement(tag);
    if (className) {
      if (Array.isArray(className)) { className.forEach(function (c) { node.classList.add(c); }); }
      else { node.className = className; }
    }
    if (html !== undefined) { node.innerHTML = html; }
    return node;
  }

  function fmtTime(iso) {
    if (!iso) { return ''; }
    var d = new Date(iso);
    if (isNaN(d.getTime())) { return ''; }
    return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  }

  function fmtBytes(n) {
    n = Number(n) || 0;
    if (n < 1024) { return n + ' B'; }
    if (n < 1048576) { return (n / 1024).toFixed(1) + ' KB'; }
    return (n / 1048576).toFixed(1) + ' MB';
  }

  /* ------------------------------------------------------------------ */
  /* router                                                              */
  /* ------------------------------------------------------------------ */

  var screenEls = {};
  var pending = {};

  function parseHash() {
    var h = window.location.hash || '#chat';
    var body = h.slice(1);
    var parts = body.split('?');
    var params = {};
    var qs = parts.slice(1).join('?');
    if (qs) {
      qs.split('&').forEach(function (kv) {
        var eq = kv.indexOf('=');
        if (eq > -1) { params[decodeURIComponent(kv.slice(0, eq))] = decodeURIComponent(kv.slice(eq + 1)); }
        else { params[decodeURIComponent(kv)] = ''; }
      });
    }
    return { screen: parts[0] || 'chat', params: params };
  }

  function screenName() {
    var p = parseHash();
    return p.screen;
  }

  function showScreen(name) {
    ['chat', 'files', 'brief', 'settings', 'pair'].forEach(function (s) {
      var node = document.getElementById('screen-' + s);
      if (node) { node.hidden = s !== name; }
    });
    document.querySelectorAll('[data-screen-link]').forEach(function (a) {
      if (a.dataset.screenLink === name) { a.setAttribute('aria-current', 'page'); }
      else { a.removeAttribute('aria-current'); }
    });
    var pairLink = document.getElementById('nav-pair');
    if (pairLink) { pairLink.hidden = name !== 'pair'; }
    var nav = document.getElementById('nav');
    if (nav) { nav.classList.remove('open'); }
    var toggle = document.getElementById('nav-toggle');
    if (toggle) { toggle.setAttribute('aria-expanded', 'false'); }
    document.title = 'Steward' + (name === 'chat' ? '' : ' - ' + name.charAt(0).toUpperCase() + name.slice(1));

    if (name === 'chat') { loadChat(); startApprovalPoll(); }
    else { stopApprovalPoll(); }
    if (name === 'files') { loadFiles(parseHash().params); }
    if (name === 'brief') { loadBrief(parseHash().params); }
    if (name === 'settings') { loadSettings(); }
    if (name === 'pair') {
      // Keep the session when the host revisits Pair.
      var pairForm = document.getElementById('pair-form');
      if (pairForm) { pairForm.hidden = false; }
      var errBox = document.getElementById('pair-error');
      if (errBox) { errBox.textContent = ''; }
      var params = parseHash().params;
      if (params.then && session.token) {
        // Already paired: go straight where the link asked (deep links).
        window.location.hash = '#' + params.then;
        return;
      }
      if (!session.token) { setStatus('Not paired yet'); }
      if (params.code && params.auto && !session.token) {
        var autoDevice = params.device || 'Phone';
        var codeInput = document.getElementById('pair-code');
        var deviceInput = document.getElementById('pair-device');
        if (codeInput) { codeInput.value = params.code; }
        if (deviceInput) { deviceInput.value = autoDevice; }
        submitPair(autoDevice, params.code, function () {
          window.location.hash = '#' + (params.then || 'chat');
        });
      }
    }
  }

  function router() {
    var target = screenName();
    var hasToken = Boolean(session.token);
    if (!hasToken && target !== 'pair') {
      // Any gated screen without a token redirects to Pair.
      showScreen('pair');
      window.location.hash = 'pair';
      return;
    }
    showScreen(target);
  }

  function setStatus(text) {
    var node = document.getElementById('rail-status');
    if (node) { node.textContent = text; }
  }

  /* ------------------------------------------------------------------ */
  /* chat                                                                */
  /* ------------------------------------------------------------------ */

  var chatState = {
    conversationId: null,
    messages: [],
    busy: false,
    approvals: []
  };
  var approvalTimer = null;

  function loadChat() {
    setStatus(' '); // clear transient status when arriving
    var params = parseHash().params;
    apiJson('/v1/conversations').then(function (data) {
      var list = data.conversations || [];
      renderConversationList(list);
      if (chatState.conversationId === null && list.length > 0) {
        // Returning to Chat resumes the most recent conversation.
        openConversation(list[0].id);
        return;
      }
      if (chatState.messages.length === 0) { renderWelcome(); }
      else { renderMessages(); }
      // Auto-send is a first-run affordance, only when nothing exists yet.
      if (params.auto && list.length === 0 && session.token && !chatState.busy) {
        var text = String(params.auto === '1' ? 'Hello' : params.auto);
        setTimeout(function () { sendMessage(text); }, 150);
      }
    }).catch(function (err) {
      if (err.status !== 401) { console.error('conversations:', err.message); }
    });
  }

  function refreshConversations() {
    if (!session.token) { return; }
    apiJson('/v1/conversations').then(function (data) {
      renderConversationList(data.conversations || []);
    }).catch(function (err) {
      if (err.status !== 401) { console.error('conversations:', err.message); }
    });
  }

  function renderConversationList(list) {
    var box = document.getElementById('conv-list');
    if (!box) { return; }
    box.textContent = '';
    if (list.length === 0) {
      box.appendChild(el('li', 'empty', 'No conversations yet'));
      return;
    }
    list.forEach(function (conv) {
      var btn = el('button', '');
      btn.addEventListener('click', function () { openConversation(conv.id); });
      var title = el('span', 'conv-title', escapeHtml(conv.title || 'Conversation'));
      var meta = el('span', 'conv-meta', escapeHtml(fmtTime(conv.created_at) + ' - ' + conv.message_count + ' messages'));
      btn.appendChild(title);
      btn.appendChild(meta);
      btn.setAttribute('aria-current', chatState.conversationId === conv.id ? 'true' : 'false');
      var item = el('li', '');
      item.appendChild(btn);
      box.appendChild(item);
    });
  }

  function renderWelcome() {
    var box = document.getElementById('chat-messages');
    if (!box) { return; }
    box.textContent = '';
    var wrap = el('div', 'welcome');
    wrap.innerHTML = '<h2>Steward is ready</h2><p>Ask anything. It runs entirely on this machine and stays here.</p>';
    box.appendChild(wrap);
  }

  function renderMessages() {
    var box = document.getElementById('chat-messages');
    if (!box) { return; }
    box.textContent = '';
    chatState.messages.forEach(function (msg) {
      box.appendChild(renderMessage(msg));
    });
    box.scrollTop = box.scrollHeight;
  }

  function renderMessage(msg) {
    var isUser = msg.role === 'user';
    var wrap = el('div', isUser ? 'msg user' : 'msg assistant');
    var meta = el('span', 'meta', isUser ? escapeHtml('You') : escapeHtml('Steward'));
    wrap.appendChild(meta);
    var body = el('div', 'mdc');
    body.innerHTML = renderMarkdown(msg.content);
    wrap.appendChild(body);
    return wrap;
  }

  function appendAssistantBubble() {
    var box = document.getElementById('chat-messages');
    if (!box) { return; }
    var wrap = el('div', 'msg assistant');
    var meta = el('span', 'meta', 'Steward');
    wrap.appendChild(meta);
    var body = el('div', 'mdc');
    body.classList.add('streaming-body');
    body.innerHTML = '';
    wrap.appendChild(body);
    var cursor = el('span', 'cursor');
    body.appendChild(cursor);
    box.appendChild(wrap);
    box.scrollTop = box.scrollHeight;
    return { wrap: wrap, body: body, cursor: cursor, text: '' };
  }

  function updateAssistantBubble(entry, delta, done) {
    entry.text += delta;
    var content = entry.text;
    if (!done) { content += '\u200b'; }
    entry.body.innerHTML = renderMarkdown(content, done);
    if (done) {
      if (entry.cursor && entry.cursor.parentNode) { entry.cursor.parentNode.removeChild(entry.cursor); }
    }
    var box = document.getElementById('chat-messages');
    if (box) { box.scrollTop = box.scrollHeight; }
  }

  function sendMessage(text) {
    if (chatState.busy) { return; }
    text = String(text || '').trim();
    if (!text) { return; }
    if (!session.token) { showScreen('pair'); return; }

    // drop the welcome view and the stale empty state
    var welcome = document.querySelector('#chat-messages .welcome');
    if (welcome && welcome.parentNode) { welcome.parentNode.removeChild(welcome); }

    chatState.messages.push({ role: 'user', content: text });
    renderMessages();

    var conversationPromise;
    if (chatState.conversationId === null) {
      conversationPromise = apiJson('/v1/conversations', { method: 'POST', body: { title: text.slice(0, 60) } })
        .then(function (conv) {
          chatState.conversationId = conv.id;
          refreshConversations();
        });
    } else {
      conversationPromise = Promise.resolve();
    }

    var entry = appendAssistantBubble();
    chatState.busy = true;
    var input = document.getElementById('chat-input');
    if (input) { input.disabled = true; }

    conversationPromise
      .then(function () {
        return api('/v1/chat', {
          method: 'POST',
          body: { text: text, conversation_id: chatState.conversationId, messages: chatState.messages.slice(0, -1) }
        });
      })
      .then(function (resp) {
        var reader = resp.body.getReader();
        var decoder = new TextDecoder();
        var buffer = '';
        function pump() {
          return reader.read().then(function (step) {
            if (step.done) { finish(); return; }
            buffer += decoder.decode(step.value, { stream: true });
            var idx;
            while ((idx = buffer.indexOf('\n\n')) !== -1) {
              var event = buffer.slice(0, idx);
              buffer = buffer.slice(idx + 2);
              event.split('\n').forEach(function (lineErr) {
                if (lineErr.indexOf('data: ') !== 0) { return; }
                var payload = lineErr.slice(6);
                if (payload === '[DONE]') { finishSoon(); return; }
                try {
                  var parsed = JSON.parse(payload);
                  if (parsed.delta) { updateAssistantBubble(entry, parsed.delta, false); }
                  if (parsed.error) { finish(); }
                } catch (e) { /* ignore malformed event */ }
              });
            }
            return pump();
          });
        }
        function finishSoon() {
          updateAssistantBubble(entry, '', true);
          finish();
        }
        function finish() {
          chatState.busy = false;
          var inputBox = document.getElementById('chat-input');
          if (inputBox) { inputBox.disabled = false; inputBox.focus(); }
          refreshConversations();
          tickApprovals();
        }
        return pump();
      })
      .catch(function (err) {
        if (err.status === 401) { return; }
        chatState.busy = false;
        var inputBox = document.getElementById('chat-input');
        if (inputBox) { inputBox.disabled = false; }
        updateAssistantBubble(entry, '', true);
        entry.body.textContent = 'Something went wrong: ' + err.message;
      });
  }

  function openConversation(id) {
    if (!session.token) { return; }
    apiJson('/v1/conversations/' + id).then(function (data) {
      chatState.conversationId = data.id;
      chatState.messages = (data.messages || []).map(function (m) {
        return { role: m.role, content: m.content };
      });
      renderMessages();
      refreshConversations();
    }).catch(function (err) {
      if (err.status !== 401) { console.error('open conversation:', err.message); }
    });
  }

  /* approvals (S9 route, merged later; the client ignores a 404) */

  function startApprovalPoll() {
    if (approvalTimer) { return; }
    tickApprovals();
    approvalTimer = setInterval(tickApprovals, 3000);
  }
  function stopApprovalPoll() {
    if (approvalTimer) { clearInterval(approvalTimer); approvalTimer = null; }
  }
  function tickApprovals() {
    if (!session.token) { return; }
    apiJson('/v1/approvals').then(function (data) {
      var rows = Array.isArray(data) ? data : (data.approvals || data.pending || []);
      renderApprovals(rows);
    }).catch(function (err) {
      if (err.status === 404) { stopApprovalPoll(); }
    });
  }
  function renderApprovals(rows) {
    var box = document.getElementById('approvals');
    if (!box) { return; }
    var pendingRows = (rows || []).filter(function (a) {
      return a && a.id != null && !a.decision;
    });
    box.textContent = '';
    if (pendingRows.length === 0) { return; }
    pendingRows.forEach(function (a) {
      var card = el('div', 'approval');
      var risk = el('span', 'risk', escapeHtml((a.risk || 'write').toUpperCase()));
      var tool = el('div', 'tool', escapeHtml(a.tool || 'tool'));
      var args = el('div', 'args', escapeHtml(JSON.stringify(a.args || {})));
      var actions = el('div', 'actions');
      var approve = el('button', 'btn approve', 'Approve');
      var deny = el('button', 'btn deny', 'Deny');
      approve.type = 'button';
      deny.type = 'button';
      approve.addEventListener('click', function () { decideApproval(a.id, 'approve'); });
      deny.addEventListener('click', function () { decideApproval(a.id, 'deny'); });
      actions.appendChild(approve);
      actions.appendChild(deny);
      card.appendChild(risk);
      card.appendChild(tool);
      card.appendChild(args);
      card.appendChild(actions);
      box.appendChild(card);
    });
  }
  function decideApproval(id, decision) {
    if (!session.token) { return; }
    apiJson('/v1/approvals/' + id, { method: 'POST', body: { decision: decision } })
      .then(tickApprovals)
      .catch(function (err) {
        if (err.status !== 401) { console.error('approval decision failed', err.message); }
      });
  }

  /* ------------------------------------------------------------------ */
  /* files                                                               */
  /* ------------------------------------------------------------------ */

  function loadFiles(params) {
    if (!session.token) { return; }
    apiJson('/v1/files').then(function (data) {
      renderIndexStatus(data);
      renderFolders(data.folders);
      if (params.q) {
        var q = params.q;
        setTimeout(function () { doSearch(q, 8); }, 100);
      }
    }).catch(function (err) {
      if (err.status !== 401) { renderIndexError(err.message); }
    });
  }

  function renderIndexError(msg) {
    var box = document.getElementById('index-status');
    if (box) { box.innerHTML = '<p class="note">' + escapeHtml(msg) + '</p>'; }
  }

  function renderIndexStatus(data) {
    var box = document.getElementById('index-status');
    if (!box) { return; }
    var idx = data.index || {};
    box.textContent = '';
    var head = el('div', 'card-head');
    var h = el('h2', '', 'Index');
    head.appendChild(h);
    var build = el('button', 'btn', 'Build index');
    build.type = 'button';
    build.addEventListener('click', function () { buildIndex(); });
    head.appendChild(build);
    box.appendChild(head);

    var dl = el('dl', 'index-dl');
    var cells = [
      ['Documents', String(idx.documents != null ? idx.documents : 0)],
      ['Chunks', String(idx.chunks != null ? idx.chunks : 0)],
      ['On disk', fmtBytes(idx.db_size_bytes)],
      ['Vector dim', String(idx.vector_dim || 0)]
    ];
    cells.forEach(function (pair) {
      dl.appendChild(el('dt', '', pair[0]));
      dl.appendChild(el('dd', '', pair[1]));
    });
    box.appendChild(dl);
    var note = '';
    if ((idx.documents || 0) === 0) {
      note = 'No documents indexed yet. Add a folder, then build the index.';
    } else if (idx.indexed_at) {
      note = 'Last indexed ' + fmtTime(new Date(idx.indexed_at * 1000).toISOString());
    }
    if (note) { box.appendChild(el('p', 'hint', escapeHtml(note))); }
  }

  function renderFolders(folders) {
    var box = document.getElementById('folder-list');
    if (!box) { return; }
    box.textContent = '';
    (folders || []).forEach(function (folder) {
      var li = el('li', '');
      var path = el('span', 'path', escapeHtml(folder));
      var remove = el('button', 'remove-folder', 'Remove');
      remove.type = 'button';
      remove.setAttribute('aria-label', 'Remove folder ' + folder);
      remove.addEventListener('click', function () { removeFolder(folder); });
      li.appendChild(path);
      li.appendChild(remove);
      box.appendChild(li);
    });
    if (!folders || folders.length === 0) {
      box.appendChild(el('li', 'empty', 'No folders configured'));
    }
  }

  function addFolder(path) {
    if (!session.token) { return; }
    apiJson('/v1/files/folders', { method: 'POST', body: { path: path } }).then(function (data) {
      renderFolders(data.folders);
      var input = document.getElementById('folder-path');
      if (input) { input.value = ''; }
      buildIndex();
    }).catch(function (err) {
      if (err.status !== 401) { renderIndexError(err.message); }
    });
  }

  function removeFolder(path) {
    if (!session.token) { return; }
    apiJson('/v1/files/folders?path=' + encodeURIComponent(path), { method: 'DELETE' }).then(function (data) {
      renderFolders(data.folders);
    }).catch(function (err) {
      if (err.status !== 401) { renderIndexError(err.message); }
    });
  }

  function buildIndex() {
    if (!session.token) { return; }
    var btn = document.querySelector('#index-status .btn');
    if (btn) { btn.disabled = true; btn.textContent = 'Building...'; }
    apiJson('/v1/files/index', { method: 'POST', body: {} }).then(function (data) {
      var s = data.summary || {};
      var label = 'Indexed ' + (s.indexed || 0) + ', reindexed ' + (s.reindexed || 0) +
        ', skipped ' + (s.skipped || 0) + ', failed ' + (s.failed || 0) +
        ' in ' + (s.seconds != null ? s.seconds + ' s' : '?') + '.';
      setStatus(label);
      loadFiles({});
    }).catch(function (err) {
      if (btn) { btn.disabled = false; btn.textContent = 'Build index'; }
      if (err.status !== 401) { renderIndexError(err.message); }
    });
  }

  function doSearch(query, k) {
    if (!session.token) { return; }
    var box = document.getElementById('search-results');
    if (box) { box.innerHTML = '<p class="empty">Searching your files...</p>'; }
    apiJson('/v1/files/search', { method: 'POST', body: { query: query, k: k || 8 } }).then(function (data) {
      renderSearchResults(data.hits || []);
    }).catch(function (err) {
      if (err.status !== 401) {
        var results = document.getElementById('search-results');
        if (results) { results.innerHTML = '<p class="empty">' + escapeHtml(err.message) + '</p>'; }
      }
    });
  }

  function renderSearchResults(hits) {
    var box = document.getElementById('search-results');
    if (!box) { return; }
    box.textContent = '';
    if (hits.length === 0) {
      box.appendChild(el('p', 'empty', 'No matches. Try another query, or build the index first.'));
      return;
    }
    hits.forEach(function (hit) {
      var card = el('div', 'hit');
      var snippet = el('div', 'hit-snippet', renderMarkdown(hit.snippet));
      card.appendChild(snippet);
      var cite = el('div', 'hit-cite');
      cite.innerHTML = 'Source <b>' + escapeHtml(hit.path || '') + '</b> at char ' +
        (hit.offset != null ? hit.offset : '?') + ' - score ' + (hit.score != null ? hit.score.toFixed(4) : '?');
      card.appendChild(cite);
      box.appendChild(card);
    });
  }

  /* ------------------------------------------------------------------ */
  /* brief                                                               */
  /* ------------------------------------------------------------------ */

  function loadBrief(params) {
    if (!session.token) { return; }
    apiJson('/v1/briefs').then(function (data) {
      renderBriefHistory(data.briefs || data.history || []);
    }).catch(function (err) {
      if (err.status === 404) { renderBriefHistory([], 'Scheduled briefs appear here once they run.'); }
      else if (err.status !== 401) { renderBriefHistory([], 'History is not available: ' + err.message); }
    });
    apiJson('/v1/briefs/today').then(function (data) {
      var text = data.today || data.text || data.brief || '';
      renderToday(text, false);
    }).catch(function (err) {
      if (err.status === 404) { renderToday('', false); }
      else if (err.status !== 401) { renderToday('', false); }
    });
    if (params && params.generate) {
      setTimeout(generateBrief, 150);
    }
  }

  function renderToday(text, onDemand) {
    var box = document.getElementById('brief-today');
    if (!box) { return; }
    var btn = document.getElementById('brief-generate');
    if (btn) { btn.disabled = false; btn.textContent = 'Generate now'; }
    if (!text) {
      box.innerHTML = '<p class="empty">No brief yet. Generate one now, or wait for the scheduled morning brief.</p>';
      return;
    }
    box.textContent = '';
    if (onDemand) {
      box.appendChild(el('p', 'hint', 'Generated on demand through your local model.'));
    }
    var body = el('div', 'mdc');
    body.innerHTML = renderMarkdown(text);
    box.appendChild(body);
  }

  function renderBriefHistory(rows, note) {
    var box = document.getElementById('brief-history');
    if (!box) { return; }
    box.textContent = '';
    if (note) { box.appendChild(el('li', 'empty', escapeHtml(note))); }
    (rows || []).forEach(function (row) {
      var li = el('li', '');
      var when = el('span', 'date', escapeHtml(fmtTime(row.date) || fmtTime(row.created_at) || ''));
      var preview = el('div', 'preview', escapeHtml(String(row.text || row.brief || '').slice(0, 200)));
      li.appendChild(when);
      li.appendChild(preview);
      box.appendChild(li);
    });
    if ((!rows || rows.length === 0) && !note) {
      box.appendChild(el('li', 'empty', 'No briefs yet'));
    }
  }

  function generateBrief() {
    if (!session.token) { return; }
    var btn = document.getElementById('brief-generate');
    if (btn) { btn.disabled = true; btn.textContent = 'Working...'; }
    apiJson('/v1/summarize', { method: 'POST', body: {} }).then(function (data) {
      renderToday(data.reply || data.brief || '', true);
    }).catch(function (err) {
      if (btn) { btn.disabled = false; btn.textContent = 'Generate now'; }
      if (err.status !== 401) {
        var box = document.getElementById('brief-today');
        if (box) { box.innerHTML = '<p class="empty">' + escapeHtml(err.message) + '</p>'; }
      }
    });
  }

  /* ------------------------------------------------------------------ */
  /* updates (one-click UI over /v1/update/check + /v1/update/apply)     */
  /* ------------------------------------------------------------------ */

  var UPDATE_AUTOCHECK_KEY = 'steward.update.autocheck';   // '1' on (default), '0' off
  var UPDATE_DISMISS_KEY = 'steward.update.dismissed.';    // + app version
  var updateState = {
    checkedAt: null,
    last: null,        // last /v1/update/check payload
    available: false,
    applying: false,
    reconnecting: false,
    error: null,
    manual: null
  };

  function hasDom() {
    return typeof document !== 'undefined';
  }

  function updateAutocheckEnabled() {
    try { return localStorage.getItem(UPDATE_AUTOCHECK_KEY) !== '0'; }
    catch (e) { return true; }   // default on, private-mode safe
  }
  function setUpdateAutocheck(on) {
    try { localStorage.setItem(UPDATE_AUTOCHECK_KEY, on ? '1' : '0'); } catch (e) { /* memory */ }
  }
  function updateDismissed(version) {
    try { return localStorage.getItem(UPDATE_DISMISS_KEY + version) === '1'; } catch (e) { return false; }
  }
  function dismissUpdate(version) {
    try { localStorage.setItem(UPDATE_DISMISS_KEY + version, '1'); } catch (e) { /* memory */ }
  }

  function latestAppVersion(result) {
    var app = (result && result.app) || {};
    return app.latest_version || null;
  }

  /** The one-line manual fallback, copied from the daemon's response. */
  function manualCommand(result) {
    return (result && result.manual_command) || 'steward update apply';
  }

  /**
   * Maybe ask the daemon for an update. Auto-check only fires when the user
   * has not turned it off; when off, no request is made at all (tested in
   * the node harness). `force` bypasses the pref for the Settings button.
   */
  function maybeCheckForUpdates(force) {
    if (!session.token) { return Promise.resolve(); }
    if (!force && !updateAutocheckEnabled()) { return Promise.resolve(); }
    return apiJson('/v1/update/check').then(function (result) {
      updateState.last = result;
      updateState.checkedAt = new Date();
      updateState.available = !!(result && result.available) ||
        !!(result && result.app && result.app.available);
      updateState.error = (result && result.ok === false) ? (result.error || 'check failed') : null;
      updateState.manual = manualCommand(result);
      renderUpdateWidget();
      return result;
    }).catch(function (err) {
      if (err.status === 401) { return null; }
      // A short daemon blip should not overwrite what we last knew.
      updateState.error = updateState.error || err.message;
      renderUpdateWidget();
      return null;
    });
  }

  function renderUpdateWidget() {
    var box = hasDom() ? document.getElementById('update-notice') : null;
    if (!box) { return; }
    var result = updateState.last || {};
    var app = result.app || {};
    var installing = updateState.applying || updateState.reconnecting;

    box.textContent = '';
    box.className = 'update-notice' + (updateState.error ? ' error' : '') +
      (installing ? ' busy' : '');
    if (!installing && updateState.error && updateState.available === false &&
        !updateState.last) {
      // check-time failure (unreachable source): quiet, one-line, no dead end
      box.hidden = false;
      var errline = el('div', 'upd-row');
      errline.appendChild(el('span', 'upd-text', 'Update check failed: ' + escapeHtml(updateState.error)));
      errline.appendChild(el('code', 'upd-cmd', escapeHtml(updateState.manual)));
      box.appendChild(errline);
      return;
    }

    var available = updateState.available && app.available;
    var version = latestAppVersion(result);
    var dismissed = version && updateDismissed(version);

    if (updateState.applying || updateState.reconnecting) {
      box.hidden = false;
      box.appendChild(renderUpdateActivity());
      return;
    }
    if (!available || dismissed) {
      box.hidden = true;
      return;
    }

    box.hidden = false;
    var head = el('div', 'upd-row');
    var icon = el('span', 'upd-icon', '\u2191');
    var title = el('span', 'upd-title', 'Steward ' + escapeHtml(version) + ' is ready to install');
    head.appendChild(icon);
    head.appendChild(title);
    box.appendChild(head);

    var notes = (result && result.app && (result.app.notes || result.app.notes_url));
    if (notes) {
      var noteLine = el('p', 'upd-notes', '');
      if (result.app.notes_url && /^https?:\/\//i.test(result.app.notes_url)) {
        noteLine.innerHTML = 'What is new: <a href="' + escapeHtml(result.app.notes_url) +
          '" target="_blank" rel="noopener">' + escapeHtml(result.app.notes) + '</a>';
      } else {
        noteLine.textContent = 'What is new: ' + notes;
      }
      box.appendChild(noteLine);
    }

    if (result.app && result.app.released) {
      box.appendChild(el('p', 'upd-meta',
        'Released ' + escapeHtml(result.app.released) +
        ' | now running ' + escapeHtml(result.app.current_version || '?')));
    }

    var actions = el('div', 'upd-actions');
    var updateBtn = el('button', 'btn primary', 'Update');
    updateBtn.type = 'button';
    updateBtn.addEventListener('click', function () { startUpdate(); });
    var laterBtn = el('button', 'btn ghost', 'Later');
    laterBtn.type = 'button';
    laterBtn.addEventListener('click', function () {
      dismissUpdate(version);
      box.hidden = true;
    });
    actions.appendChild(updateBtn);
    actions.appendChild(laterBtn);
    box.appendChild(actions);
  }

  function renderUpdateActivity() {
    var wrap = el('div', 'upd-activity');
    if (updateState.reconnecting) {
      wrap.appendChild(el('p', 'upd-title', 'Update installed. Reconnecting to Steward\u2026'));
      var spin = el('div', 'upd-progress indeterminate'); spin.setAttribute('aria-hidden', 'true');
      wrap.appendChild(spin);
    } else {
      wrap.appendChild(el('p', 'upd-title', updateState.applying === 'final' ? 'Almost done\u2026' :
        'Downloading Steward ' + escapeHtml(latestAppVersion(updateState.last) || '') + '\u2026'));
      var bar = el('div', 'upd-progress');
      var fill = el('div', 'upd-progress-fill');
      bar.appendChild(fill);
      wrap.appendChild(bar);
      var label = el('p', 'upd-meta', updateState.applying === true ? 'Verifying and installing\u2026' : '');
      wrap.appendChild(label);
    }
    return wrap;
  }

  /**
   * One-click update to the newest signed build. Streams real download
   * progress from POST /v1/update/apply (Accept: text/event-stream), then:
   * applied + restart_intent -> show reconnecting, poll /health on its own,
   * and land back in Chat with history intact; refusal/error -> show the real
   * reason and the manual command, never a silent no-op.
   */
  function startUpdate() {
    if (!session.token || updateState.applying || updateState.reconnecting) { return; }
    updateState.applying = true;
    updateState.error = null;
    renderUpdateWidget();

    var headers = { 'Content-Type': 'application/json' };
    if (session.token) { headers['Authorization'] = 'Bearer ' + session.token; }
    fetch('/v1/update/apply', {
      method: 'POST',
      headers: Object.assign({ Accept: 'text/event-stream' }, headers),
      body: '{}'
    }).then(function (resp) {
      if (resp.status === 401) {
        var e401 = new Error('unauthorized');
        e401.status = 401;
        return handleUnauthorized().then(function () { throw e401; });
      }
      if (!resp.ok) {
        return resp.text().then(function (t) {
          var err = new Error(t || ('apply failed (' + resp.status + ')'));
          err.status = resp.status;
          throw err;
        });
      }
      return resp;
    }).then(function (resp) {
      var reader = resp.body.getReader();
      var decoder = new TextDecoder();
      var buffer = '';
      var progressFill = null;
      function setProgress(done, total) {
        if (!progressFill) {
          progressFill = document.querySelector('#update-notice .upd-progress-fill');
        }
        if (progressFill && total > 0) {
          progressFill.style.width = Math.min(100, Math.round(done / total * 100)) + '%';
        }
      }
      function pump() {
        return reader.read().then(function (step) {
          if (step.done) { return; }
          buffer += decoder.decode(step.value, { stream: true });
          var idx;
          while ((idx = buffer.indexOf('\n\n')) !== -1) {
            var event = buffer.slice(0, idx);
            buffer = buffer.slice(idx + 2);
            event.split('\n').forEach(function (line) {
              if (line.indexOf('data: ') !== 0) { return; }
              var payload = line.slice(6);
              if (payload === '[DONE]') { return; }
              var ev;
              try { ev = JSON.parse(payload); } catch (e) { return; }
              if (ev.event === 'progress') { setProgress(ev.done, ev.total); }
              if (ev.event === 'phase' && ev.name === 'apply') {
                // download done; moving into verify/apply
                var bar = document.querySelector('#update-notice .upd-activity .upd-meta');
                if (bar) { bar.textContent = 'Verifying and installing\u2026'; }
              }
              if (ev.event === 'result') { finishUpdate(ev.result); }
            });
          }
          return pump();
        });
      }
      function finishUpdate(result) {
        updateState.applying = false;
        updateState.last = Object.assign({}, updateState.last, result);
        updateState.manual = manualCommand(result);
        if (result && result.ok === true && result.state === 'applied' &&
            result.restart_intent && result.restart_intent.pending) {
          // Daemon is replacing itself; show reconnect and let it come back.
          updateState.reconnecting = true;
          renderUpdateWidget();
          waitForDaemonBack(result.restart_intent.version, result);
        } else {
          updateState.error = (result && result.error) ||
            ((result && result.state === 'noop') ? 'Already on the newest version.' :
             'The update did not apply.');
          updateState.available = false;
          renderUpdateWidget();
          renderFinalFailure(result);
        }
      }
      function renderFinalFailure(result) {
        var box = document.getElementById('update-notice');
        if (!box) { return; }
        box.hidden = false;
        var reason = (result && result.error) || updateState.error;
        box.appendChild(el('p', 'upd-notes', 'The update failed: ' + escapeHtml(reason)));
        if (updateState.manual) {
          box.appendChild(el('p', 'upd-manual', 'Run it from a terminal on this machine:'));
          box.appendChild(el('code', 'upd-cmd', escapeHtml(updateState.manual)));
        }
        var retry = el('button', 'btn', 'Try update again');
        retry.type = 'button';
        retry.addEventListener('click', function () {
          updateState.available = true; updateState.error = null;
          renderUpdateWidget();
          if (!updateState.applying) { startUpdate(); }
        });
        var dismiss = el('button', 'btn ghost', 'Dismiss');
        dismiss.type = 'button';
        dismiss.addEventListener('click', function () { box.hidden = true; });
        var actions = el('div', 'upd-actions');
        actions.appendChild(retry);
        actions.appendChild(dismiss);
        box.appendChild(actions);
      }
      function waitForDaemonBack(targetVersion, result) {
        // Keep the message area; poll /health until the daemon answers with
        // the new version. History is server-side and the session token
        // survives restart, so re-entering Chat resumes it intact.
        var deadline = Date.now() + 120000;   // 2 minutes, generous for a swap
        var poll = function () {
          if (!updateState.reconnecting) { return; }
          fetch('/health').then(function (resp) {
            if (!resp.ok) { return schedule(); }
            return resp.json().then(function (h) {
              var version = h && h.version ? String(h.version) : '';
              if (targetVersion && version && version !== targetVersion &&
                  Date.now() < deadline) {
                return schedule();   // old daemon mid-shutdown; keep waiting
              }
              if (version === targetVersion || Date.now() >= deadline) {
                finishReconnect(targetVersion);
              } else {
                schedule();
              }
            });
          }).catch(schedule);
          function schedule() {
            if (Date.now() < deadline) { setTimeout(poll, 1000); }
            else { finishReconnect(targetVersion, true); }
          }
        };
        poll();
      }
      function finishReconnect(targetVersion, timedOut) {
        updateState.reconnecting = false;
        updateState.available = false;
        updateState.error = null;
        renderUpdateWidget();
        // Land back in chat with history intact: conversations live on the
        // daemon and our token is still valid, so a light reload resumes.
        window.location.hash = '#chat';
        window.location.reload();
      }
      return pump();
    }).catch(function (err) {
      if (err.status === 401) { return; }
      updateState.applying = false;
      updateState.reconnecting = false;
      updateState.error = err.message || 'update failed';
      renderUpdateWidget();
      var box = document.getElementById('update-notice');
      if (box) {
        box.hidden = false;
        box.appendChild(el('p', 'upd-notes', 'The update failed: ' + escapeHtml(err.message)));
        if (updateState.manual) {
          box.appendChild(el('p', 'upd-manual', 'Run it from a terminal on this machine:'));
          box.appendChild(el('code', 'upd-cmd', escapeHtml(updateState.manual)));
        }
      }
    });
  }

  function renderUpdateSettings(info) {
    var fields = document.getElementById('update-fields');
    if (!fields) { return; }
    fields.textContent = '';
    function row(label, html) {
      fields.appendChild(el('dt', '', label));
      var dd = el('dd', '');
      dd.innerHTML = html;
      fields.appendChild(dd);
    }
    var appV = ((updateState.last && updateState.last.app) || {}).current_version || info.version;
    var daemonV = info.version;
    row('App version', escapeHtml(appV || '?'));
    row('Daemon version', escapeHtml(daemonV || '?'));

    // Check for updates button row
    var checkCell = el('dd', 'upd-check-cell');
    var checkBtn = el('button', 'btn', 'Check for updates');
    checkBtn.type = 'button';
    checkBtn.addEventListener('click', function () {
      checkBtn.disabled = true;
      checkBtn.textContent = 'Checking\u2026';
      maybeCheckForUpdates(true).finally(function () {
        checkBtn.disabled = false;
        checkBtn.textContent = 'Check for updates';
        if (updateState.available) {
          renderUpdateWidget();   // show the banner too
        }
      });
    });
    fields.appendChild(el('dt', '', 'Check for updates'));
    checkCell.appendChild(checkBtn);
    var last = updateState.last ? 'Last checked ' + updateState.checkedAt.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : 'Never checked';
    checkCell.appendChild(el('span', 'hint', ' ' + last));
    fields.appendChild(checkCell);

    // Auto-check toggle (default on)
    var auto = updateAutocheckEnabled();
    var toggleCell = el('dd', 'upd-check-cell');
    var toggleLabel = el('label', 'toggle');
    var checkbox = el('input', '');
    checkbox.type = 'checkbox';
    checkbox.checked = auto;
    checkbox.setAttribute('aria-label', 'Check for updates when the app opens');
    checkbox.addEventListener('change', function () {
      setUpdateAutocheck(checkbox.checked);
    });
    toggleLabel.appendChild(checkbox);
    toggleLabel.appendChild(el('span', 'toggle-track', ''));
    toggleCell.appendChild(toggleLabel);
    toggleCell.appendChild(el('span', 'hint', auto ? ' On (when this app opens)' : ' Off'));
    fields.appendChild(el('dt', '', 'Auto-check'));
    fields.appendChild(toggleCell);
  }

  /* ------------------------------------------------------------------ */
  /* settings                                                            */
  /* ------------------------------------------------------------------ */

  function loadSettings() {
    if (!session.token) { return; }
    apiJson('/v1/webapp/info').then(function (info) {
      renderSettings(info);
      renderUpdateSettings(info);
    }).catch(function (err) {
      if (err.status !== 401) { console.error('settings:', err.message); }
    });
    apiJson('/health').then(function (health) {
      setStatus((health.status === 'ok' ? 'Connected' : 'Degraded') + ' - ' + (health.model || ''));
    }).catch(function () { setStatus('Disconnected'); });
  }

  function renderSettings(info) {
    var fields = document.getElementById('settings-fields');
    if (fields) {
      fields.textContent = '';
      var rows = [
        ['Model', info.model + ' (resident assistant)'],
        ['Backend', info.backend_mode + (info.backend_version ? ' ' + info.backend_version : '')],
        ['Status', info.backend_ok ? 'Healthy' : 'Needs attention'],
        ['Data directory', info.data_dir],
        ['Telemetry', '<span class="telemetry"><span class="dot"></span>Off, permanently</span>'],
        ['License', 'Free, perpetual, no subscription'],
        ['This device', info.device]
      ];
      rows.forEach(function (pair) {
        fields.appendChild(el('dt', '', pair[0]));
        var dd = el('dd', pair[0] === 'Data directory' ? 'mono' : '');
        dd.className = pair[0] === 'Data directory' ? 'mono' : '';
        dd.innerHTML = pair[1];
        fields.appendChild(dd);
      });
    }
    var about = document.getElementById('about-fields');
    if (about) {
      about.textContent = '';
      var aboutRows = [
        ['Version', info.version],
        ['Pairing link', info.pairing_url]
      ];
      aboutRows.forEach(function (pair) {
        about.appendChild(el('dt', '', pair[0]));
        var dd = el('dd', 'mono', pair[1]);
        dd.className = 'mono';
        about.appendChild(dd);
      });
    }
    var url = document.getElementById('pair-url');
    if (url && info.pairing_url) { url.textContent = info.pairing_url; }
    var canvas = document.getElementById('pair-qr');
    if (canvas && info.pairing_url) {
      drawQr(canvas, info.pairing_url);
    }
  }

  /* ------------------------------------------------------------------ */
  /* pair                                                                */
  /* ------------------------------------------------------------------ */

  function maybeAutoPair(/* retained for hash-param callers */) {
    // The auto-pair flow now lives in showScreen('pair'); this empty hook
    // exists so any older inline references are inert.
  }

  function submitPair(device, code, onSuccess) {
    var errBox = document.getElementById('pair-error');
    if (errBox) { errBox.textContent = ''; }
    var submit = document.querySelector('#pair-form .btn.primary');
    if (submit) { submit.disabled = true; }
    fetch('/v1/pair', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code: String(code || '').trim(), device: String(device || '').trim() })
    }).then(function (resp) {
      if (resp.status === 401) {
        if (errBox) { errBox.textContent = 'That code was not accepted.'; }
        return null;
      }
      if (resp.status === 429) {
        return resp.json().then(function (data) {
          if (errBox) { errBox.textContent = 'Too many attempts. Wait ' + (data.retry_after_seconds || 60) + ' seconds.'; }
        });
      }
      if (!resp.ok) { return resp.json().then(function () { if (errBox) { errBox.textContent = 'Pairing failed. Try again.'; } }); }
      return resp.json().then(function (data) {
        session.token = data.token;
        session.device = data.device || device;
        saveSession(session);
        if (onSuccess) { onSuccess(); }
      });
    }).finally(function () {
      if (submit) { submit.disabled = false; }
    });
  }

  function revealPairCode() {
    fetch('/pair/code').then(function (resp) {
      if (!resp.ok) {
        var btn = document.getElementById('pair-show-code');
        if (btn) { btn.hidden = true; }
        var errBox = document.getElementById('pair-error');
        if (errBox) {
          errBox.textContent = 'The pairing code is only served to the host computer, so this browser cannot read it.';
        }
        return;
      }
      return resp.json().then(function (data) {
        if (data.code && data.code.length === 6) {
          var codeInput = document.getElementById('pair-code');
          if (codeInput) { codeInput.value = data.code; }
          var errBox = document.getElementById('pair-error');
          if (errBox) { errBox.textContent = 'Code filled in automatically.'; }
        }
      });
    }).catch(function () { /* loopback only, quiet */ });
  }

  /* ------------------------------------------------------------------ */
  /* wiring                                                              */
  /* ------------------------------------------------------------------ */

  function autoGrow(textarea) {
    textarea.style.height = 'auto';
    textarea.style.height = Math.min(textarea.scrollHeight, 160) + 'px';
  }

  function init() {
    if (typeof document === 'undefined') { return; }

    var toggle = document.getElementById('nav-toggle');
    var nav = document.getElementById('nav');
    if (toggle && nav) {
      toggle.addEventListener('click', function () {
        var open = nav.classList.toggle('open');
        toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
      });
    }

    var pairLink = document.getElementById('nav-pair');
    if (pairLink) { pairLink.hidden = session.token !== ''; }

    var chatForm = document.getElementById('chat-form');
    if (chatForm) {
      chatForm.addEventListener('submit', function (e) {
        e.preventDefault();
        var input = document.getElementById('chat-input');
        if (!input) { return; }
        sendMessage(input.value);
        input.value = '';
        autoGrow(input);
      });
    }
    var chatInput = document.getElementById('chat-input');
    if (chatInput) {
      chatInput.addEventListener('input', function () { autoGrow(chatInput); });
      chatInput.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' && !e.shiftKey && !isComposingEvent(e)) {
          e.preventDefault();
          chatForm.dispatchEvent(new Event('submit', { cancelable: true }));
        }
      });
    }
    function isComposingEvent(e) {
      return e.isComposing === true;
    }

    var newChat = document.getElementById('new-chat');
    if (newChat) {
      newChat.addEventListener('click', function () {
        if (!session.token) { router(); return; }
        chatState.conversationId = null;
        chatState.messages = [];
        renderWelcome();
        if (chatInput) { chatInput.focus(); }
      });
    }

    var addFolderForm = document.getElementById('add-folder');
    if (addFolderForm) {
      addFolderForm.addEventListener('submit', function (e) {
        e.preventDefault();
        var input = document.getElementById('folder-path');
        if (!input || !input.value.trim()) { return; }
        addFolder(input.value.trim());
      });
    }

    var searchForm = document.getElementById('search-form');
    if (searchForm) {
      searchForm.addEventListener('submit', function (e) {
        e.preventDefault();
        var input = document.getElementById('search-query');
        if (!input || !input.value.trim()) { return; }
        doSearch(input.value.trim(), 8);
      });
    }

    var briefGenerate = document.getElementById('brief-generate');
    if (briefGenerate) { briefGenerate.addEventListener('click', generateBrief); }

    var pairForm = document.getElementById('pair-form');
    if (pairForm) {
      pairForm.addEventListener('submit', function (e) {
        e.preventDefault();
        var device = document.getElementById('pair-device');
        var code = document.getElementById('pair-code');
        if (!device || !code) { return; }
        submitPair(device.value, code.value, function () {
          window.location.hash = '#chat';
        });
      });
    }
    var showCode = document.getElementById('pair-show-code');
    if (showCode) {
      showCode.addEventListener('click', revealPairCode);
      // Only a host browser can read the code; probe and reveal the button.
      fetch('/pair/code').then(function (resp) {
        showCode.hidden = !resp.ok;
      }).catch(function () { showCode.hidden = true; });
    }

    window.addEventListener('hashchange', router);
    setStatus('Connecting');
    router();
    // Auto-check for updates once per page load (respects the toggle; the
    // daemon also checks at boot, so a fresh check here is cheap and shows
    // the banner the instant the user opens the app).
    if (session.token && updateAutocheckEnabled()) {
      maybeCheckForUpdates(false);
    }
  }

  /* ------------------------------------------------------------------ */

  NS.Steward = {
    escapeHtml: escapeHtml,
    renderMarkdown: renderMarkdown,
    qrMatrixEncode: qrMatrixEncode,
    drawQr: drawQr,
    updateAutocheckEnabled: updateAutocheckEnabled,
    setUpdateAutocheck: setUpdateAutocheck,
    maybeCheckForUpdates: maybeCheckForUpdates,
    // Test/headless surface: drive the update logic without a daemon or DOM.
    _test: {
      setSessionToken: function (t) { session.token = String(t || ''); },
      getUpdateState: function () { return updateState; },
      startUpdate: startUpdate
    },
    debug: function () {
      return {
        authed: Boolean(session && session.token),
        screen: screenName(),
        hash: (typeof window !== 'undefined') ? window.location.hash : '',
        chatBusy: chatState.busy,
        chatMessages: chatState.messages.length,
        conversationId: chatState.conversationId
      };
    }
  };

  if (typeof document !== 'undefined') {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', init);
    } else {
      init();
    }
  }
})();
