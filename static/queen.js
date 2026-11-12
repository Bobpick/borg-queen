
let isRunning = false;
let currentQueryResult = null;
let queryInFlight = false;

// v2.6: surface ANY silent browser-side failure on-page instead of a dead button.
window.addEventListener('error', function(ev) {
  try {
    const banner = document.getElementById('js-error-banner');
    const txt = document.getElementById('js-error-text');
    if (banner && txt) {
      txt.innerText = (ev.message || 'unknown error') + ' @ ' + (ev.filename || '').split('/').pop() + ':' + (ev.lineno || '?');
      banner.style.display = 'block';
    }
    txLog('Browser script error: ' + (ev.message || ev.error));
  } catch(e) { console.error(e); }
});
window.addEventListener('unhandledrejection', function(ev) {
  try { txLog('Unhandled promise rejection: ' + (ev.reason && ev.reason.message ? ev.reason.message : ev.reason)); }
  catch(e) { console.error(e); }
});

// v2.6: TX request-state badge + status line — single source of truth for
// "did my TRANSMIT register?" States: idle|sending|waiting|done|error
function setTxState(state, detailText) {
  const badge = document.getElementById('tx-badge');
  const line = document.getElementById('tx-status-line');
  const labels = { idle: 'TX: IDLE', sending: 'TX: SENDING', waiting: 'TX: WAITING', done: 'TX: DONE', error: 'TX: ERROR' };
  if (badge) {
    badge.className = 'tx-badge tx-' + state;
    badge.innerText = labels[state] || ('TX: ' + state.toUpperCase());
  }
  if (line) {
    line.className = 'tx-status-line' + (state === 'done' ? ' done' : state === 'error' ? ' error' : '');
    if (detailText !== undefined && detailText !== null) line.innerText = detailText;
  }
}

// v2.6: append a local [BROWSER]-stamped line to the telemetry feed so every
// TRANSMIT click leaves a visible trail even if the server never responds.
function txLog(message) {
  try {
    const feed = document.getElementById('terminal-feed');
    if (!feed) return;
    const row = document.createElement('div');
    row.className = 'log-entry';
    const now = new Date().toTimeString().slice(0, 8);
    row.innerHTML = '<span class="log-time">[' + now + ']</span><span class="log-caste caste-query">TX</span>' + message;
    feed.appendChild(row);
    feed.scrollTop = feed.scrollHeight;
  } catch(e) { console.error(e); }
}

async function fetchStatus() {
  try {
    const res = await fetch('/api/status');
    const data = await res.json();
    
    document.getElementById('val-notes').innerText = data.total_notes.toLocaleString();
    document.getElementById('val-pending').innerText = data.pending_studies;
    document.getElementById('val-assimilated').innerText = data.processed_today;
    updateHostMeters(data.host);
    document.getElementById('inbox-path-display').innerText = "Inbox: " + data.inbox_path;
    document.getElementById('live-step-badge').innerText = "STATUS: " + data.current_step;

    const ollamaDot = document.getElementById('ollama-dot');
    const ollamaText = document.getElementById('ollama-text');
    if (data.ollama_online) {
      ollamaDot.className = "pill-dot";
      ollamaText.innerText = "OLLAMA ENGINE ONLINE";
    } else {
      ollamaDot.className = "pill-dot offline";
      ollamaText.innerText = "SIMULATION / AUTONOMOUS MODE";
    }

    const feed = document.getElementById('terminal-feed');
    feed.innerHTML = "";
    data.logs.forEach(log => {
      const row = document.createElement('div');
      row.className = "log-entry";
      let casteClass = "caste-system";
      const c = log.caste.toLowerCase();
      if (c.includes("scout")) casteClass = "caste-scout";
      else if (c.includes("drone")) casteClass = "caste-drone";
      else if (c.includes("adversary")) casteClass = "caste-adversary";
      else if (c.includes("queen")) casteClass = "caste-queen";
      else if (c.includes("index")) casteClass = "caste-index";
      else if (c.includes("query")) casteClass = "caste-query";

      row.innerHTML = `<span class="log-time">[${log.timestamp}]</span><span class="log-caste ${casteClass}">${log.caste}</span>${log.message}`;
      feed.appendChild(row);
    });
    feed.scrollTop = feed.scrollHeight;

    updatePreloadUI(data);

    highlightPipeline(data.current_step);

    const btn = document.getElementById('btn-assimilate');
    const btnLabel = document.getElementById('btn-label');
    isRunning = data.is_running;
    if (isRunning) {
      btn.disabled = true;
      btnLabel.innerText = "ASSIMILATING INBOX...";
    } else {
      btn.disabled = false;
      btnLabel.innerText = "ASSIMILATE INBOX STUDIES";
    }

  } catch(e) {
    console.error("Status fetch error", e);
  }
}

function fillHwBar(id, pct) {
  const bar = document.getElementById(id);
  if (!bar) return;
  const span = bar.querySelector('span');
  const n = (pct == null || isNaN(pct)) ? 0 : Math.max(0, Math.min(100, pct));
  if (span) span.style.width = n + '%';
  bar.className = 'hw-bar' + (n >= 90 ? ' hot' : n >= 70 ? ' warn' : '');
}

function updateHostMeters(host) {
  if (!host) return;
  const cpuEl = document.getElementById('val-cpu');
  const cpuSub = document.getElementById('sub-cpu');
  if (cpuEl) cpuEl.innerText = (host.cpu_pct == null ? '--' : host.cpu_pct) + '%';
  if (cpuSub) {
    const cpuTemp = host.cpu_temp_c == null ? '' : (host.cpu_temp_c + '°C · ');
    cpuSub.innerText = cpuTemp + 'load ' + (host.load1 == null ? '--' : host.load1) + ' / ' + (host.nproc || '--') + ' cores';
  }
  fillHwBar('bar-cpu', host.cpu_pct);

  const gpu = host.gpu || {};
  const gpuEl = document.getElementById('val-gpu');
  const gpuSub = document.getElementById('sub-gpu');
  if (gpuEl) gpuEl.innerText = (gpu.util_pct == null ? '--' : gpu.util_pct) + '%';
  if (gpuSub) {
    const vu = gpu.vram_used_gb != null ? gpu.vram_used_gb : '--';
    const vt = gpu.vram_total_gb != null ? gpu.vram_total_gb : '--';
    const gpuTemp = gpu.temp_c == null ? '' : (gpu.temp_c + '°C · ');
    gpuSub.innerText = gpuTemp + (gpu.name || 'GPU') + '  ' + vu + '/' + vt + ' GB';
  }
  fillHwBar('bar-gpu', gpu.util_pct != null ? gpu.util_pct : gpu.pct);

  const mem = host.mem || {};
  const ramEl = document.getElementById('val-ram');
  const ramSub = document.getElementById('sub-ram');
  const chamber = document.getElementById('val-chamber');
  if (ramEl) ramEl.innerText = (mem.used_gb == null ? '--' : mem.used_gb) + ' GB';
  if (ramSub) ramSub.innerText = 'of ' + (mem.total_gb == null ? '--' : mem.total_gb) + ' GB  (' + (mem.pct == null ? '--' : mem.pct) + '%)';
  if (chamber && mem.total_gb) chamber.innerText = Math.round(mem.total_gb) + ' GB';
  fillHwBar('bar-ram', mem.pct);
}

// ---- v2.5 VRAM Preload logic ----
let preloadKnownHot = new Set();
let autoPreloadFired = false;

function selectedModel() {
  return document.getElementById('query-model-select').value;
}

function modelShortName(m) {
  return (m || '').split(':')[0].split('/').pop();
}

function isModelHot(model, loadedList) {
  if (preloadKnownHot.has(model)) return true;
  const short = modelShortName(model).toLowerCase();
  return (loadedList || []).some(l => (l || '').toLowerCase().includes(short));
}

function updatePreloadUI(statusData) {
  const model = selectedModel();
  const dot = document.getElementById('preload-dot');
  const label = document.getElementById('preload-status');
  const btn = document.getElementById('btn-preload');
  if (!dot || !label || !btn) return;

  const loaded = statusData.loaded_models || [];
  const loading = statusData.preloading || [];
  const short = modelShortName(model).toLowerCase();
  const isLoading = loading.some(l => (l || '').toLowerCase().includes(short)) || loading.includes(model);

  if (isModelHot(model, loaded)) {
    dot.className = "preload-dot hot";
    label.className = "preload-status hot";
    label.innerText = "● HOT";
    btn.disabled = false;
    btn.innerHTML = "🔥 PRELOAD " + short.toUpperCase().slice(0, 9);
  } else if (isLoading) {
    dot.className = "preload-dot loading";
    label.className = "preload-status loading";
    label.innerText = "◌ LOADING";
    btn.disabled = true;
    btn.innerHTML = "⏳ LOADING";
  } else {
    dot.className = "preload-dot";
    label.className = "preload-status";
    label.innerText = "○ COLD";
    btn.disabled = false;
    btn.innerHTML = "🔥 PRELOAD " + short.toUpperCase().slice(0, 9);
  }

  // Queen chamber card reflects residency too
  const queenStatus = document.getElementById('queen-status');
  if (queenStatus) {
    if (isModelHot(model, loaded)) queenStatus.innerText = modelShortName(model) + " HOT // Transmit Ready";
    else if (isLoading) queenStatus.innerText = "Loading " + modelShortName(model) + " into VRAM...";
  }
}

async function preloadModel(silent) {
  const model = selectedModel();
  const btn = document.getElementById('btn-preload');
  const dot = document.getElementById('preload-dot');
  const label = document.getElementById('preload-status');
  try {
    if (!silent) {
      btn.disabled = true;
      btn.innerHTML = "⏳ LOADING";
      dot.className = "preload-dot loading";
      label.className = "preload-status loading";
      label.innerText = "◌ LOADING";
    }
    const res = await fetch('/api/preload', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: model })
    });
    const data = await res.json();
    if (data.status === 'hot') {
      preloadKnownHot.add(model);
      if (!silent) alert(model + " is already HOT in VRAM/RAM — Transmit will answer instantly.");
    } else if (data.status === 'offline') {
      if (!silent) alert("Ollama is offline. Start it with `ollama serve`, then press PRELOAD again.");
      btn.disabled = false;
    } else if (data.status === 'loading') {
      // v2.6: never touch the answer box from preload — progress shows on the
      // preload dot + telemetry feed only.
      txLog('Preloading ' + model + ' in background — watch the ◌ LOADING dot turn ● HOT.');
      if (!silent) {
        console.log("Preloading " + model + " in background...");
      }
    }
    fetchStatus();
  } catch(e) {
    console.error("Preload error", e);
    if (!silent) alert("Preload error: " + e);
    btn.disabled = false;
  }
}

function onModelChange() {
  // Switching cores auto-pins the newly selected model HOT (evicts nothing manually;
  // Ollama keeps up to OLLAMA_MAX_LOADED_MODELS resident).
  preloadModel(true);
}

async function executeHiveQuery() {
  // ---- v2.6 TRANSMIT ASSURANCE ----
  // Feedback FIRST, synchronously, before anything that could throw — so a
  // click can never again produce "no indication the request has been made".
  const btn = document.getElementById('btn-queen-query');
  const input = document.getElementById('queen-prompt-input');
  const modelSelect = document.getElementById('query-model-select');
  const resBox = document.getElementById('query-result-box');
  const answerEl = document.getElementById('result-answer');
  const sourcesCont = document.getElementById('sources-tags-container');

  const clickStamp = new Date().toTimeString().slice(0, 8);
  if (resBox) resBox.style.display = "block";
  if (answerEl) { answerEl.classList.remove('error'); }
  setTxState('sending', '▶ [' + clickStamp + '] TRANSMIT clicked — dispatching request...');
  txLog('TRANSMIT clicked — dispatching request to /api/query');

  let query = "";
  let model = "cogito:32b";
  try {
    query = (input && input.value ? input.value : "").trim();
    model = (modelSelect && modelSelect.value) ? modelSelect.value : model;
  } catch(e) {
    setTxState('error', '✖ Could not read prompt input: ' + e);
    return;
  }

  if (!query) {
    setTxState('idle', '⚠ Type a question in the prompt box first, then press TRANSMIT.');
    if (answerEl) answerEl.innerText = "⚠ Empty query — nothing was sent. Type a research question above and press TRANSMIT (or Enter).";
    txLog('Empty query — nothing sent.');
    return;
  }

  if (queryInFlight) {
    setTxState('waiting', '⏳ A query is already in flight — please wait for it to finish.');
    txLog('TRANSMIT ignored: previous query still in flight.');
    return;
  }
  queryInFlight = true;

  btn.disabled = true;
  btn.innerHTML = "<span>⚙️ SENDING...</span>";
  if (answerEl) answerEl.innerText = "Dispatching to " + model + " — FTS5 vault search (~3ms) then Queen synthesis...";
  if (sourcesCont) sourcesCont.innerHTML = "";

  const t0 = Date.now();
  // Live elapsed ticker: proves every second that the request is in flight,
  // even across a 60-90s cold 14B load.
  const ticker = setInterval(() => {
    const s = Math.floor((Date.now() - t0) / 1000);
    btn.innerHTML = "<span>⏳ WAITING " + s + "s...</span>";
    setTxState('waiting', '⏳ [' + clickStamp + '] Request in flight to ' + model + ' — ' + s + 's elapsed. ' +
      (s < 12 ? 'FTS search + model warmup...' : s < 60 ? 'Queen is reasoning (~20 t/s). Cold 14B loads take 60-90s — GPU spike is normal.' : 'Still working — large cold loads can take ~2 min. Next time press 🔥 PRELOAD first for instant answers.'));
  }, 1000);

  try {
    setTxState('sending', '▶ [' + clickStamp + '] POST /api/query (stream) → ' + model + ' ...');
    const res = await fetch('/api/query', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Accept': 'text/event-stream' },
      body: jsonStringify({ query: query, model: model, stream: true })
    });
    if (!res.ok) {
      let errBody = null;
      try { errBody = await res.json(); } catch (e) {}
      renderSources(errBody && errBody.sources ? errBody.sources : []);
      throw new Error('HTTP ' + res.status + ': ' + ((errBody && errBody.error) ? errBody.error : 'unknown server error'));
    }

    const ctype = (res.headers.get('Content-Type') || '');
    let data = null;
    if (ctype.indexOf('event-stream') !== -1 && res.body) {
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = '';
      let answer = '';
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const blocks = buf.split('\n\n');
        buf = blocks.pop();
        for (let i = 0; i < blocks.length; i++) {
          const line = blocks[i].split('\n').filter(function (l) { return l.indexOf('data:') === 0; })
            .map(function (l) { return l.replace(/^data:\s?/, ''); }).join('');
          if (!line) continue;
          let ev;
          try { ev = JSON.parse(line); } catch (e) { continue; }
          if (ev.event === 'meta' || ev.event === 'plan') {
            renderSources(ev.sources || []);
            if (ev.event === 'plan' && ev.parts && ev.parts.length) {
              const labels = ev.parts.map(function (p, i) { return (i + 1) + '. ' + p.title; }).join(' · ');
              setTxState('waiting', '⏳ [' + clickStamp + '] ' + ev.parts.length + ' sections: ' + labels);
              if (answerEl && !answer) answerEl.innerText = 'Sectioned retrieval — ' + ev.parts.length + ' parts, ' + (ev.sources ? ev.sources.length : 0) + ' notes…';
            } else if (answerEl && !answer) {
              answerEl.innerText = 'Queen is generating… tokens will appear here.';
            }
          } else if (ev.event === 'section') {
            setTxState('waiting', '⏳ [' + clickStamp + '] Section ' + ev.index + '/' + ev.total + ' — ' + ev.title);
          } else if (ev.event === 'token') {
            answer += (ev.text || '');
            if (answerEl) { answerEl.classList.remove('error'); answerEl.innerText = answer; }
            setTxState('waiting', '⏳ [' + clickStamp + '] Streaming from ' + model + ' — ' + Math.floor((Date.now() - t0) / 1000) + 's');
          } else if (ev.event === 'error') {
            throw new Error(ev.error || 'stream error');
          } else if (ev.event === 'done') {
            data = ev;
            answer = ev.answer || answer;
          }
        }
      }
      if (!data) data = { answer: answer, model: model, sources: [] };
    } else {
      try {
        data = await res.json();
      } catch (parseErr) {
        throw new Error('Server returned non-JSON response (HTTP ' + res.status + '). Is the Queen server still running?');
      }
    }
    if (data.error) {
      renderSources(data.sources || []);
      throw new Error(data.error);
    }

    currentQueryResult = data;
    const elapsed = (data.elapsed_s !== undefined) ? data.elapsed_s + 's' : Math.round((Date.now() - t0) / 100) / 10 + 's';
    if (answerEl) answerEl.innerText = data.answer || "(Empty answer returned by model)";
    renderSources(data.sources || []);
    setTxState('done', '✔ [' + clickStamp + '] Answer received in ' + elapsed + ' via ' + (data.model || model) +
      ' — ' + (data.sources ? data.sources.length : 0) + ' vault sources cited.');
    txLog('Answer received in ' + elapsed + ' (' + (data.sources ? data.sources.length : 0) + ' sources).');
    try { preloadKnownHot.add(model); } catch(e) {}

  } catch(e) {
    const s = Math.round((Date.now() - t0) / 100) / 10;
    if (answerEl) {
      answerEl.classList.add('error');
      answerEl.innerText = "✖ TRANSMIT FAILED after " + s + "s: " + (e && e.message ? e.message : e) +
        "\n\nTriage:\n1. Header should read v2.15 HEAVY. Ctrl+Shift+R after restart.\n" +
        "2. 70B/72B: first token can take 6+ minutes on RAM. Wait for streaming; do not re-click.\n" +
        "3. Interactive Q&A: cogito:32b. 70B/72B is a single deep pass (not 4 sections).\n" +
        "4. curl -s http://localhost:8082/api/diag";
    }
    setTxState('error', '✖ [' + clickStamp + '] TRANSMIT FAILED after ' + s + 's: ' + (e && e.message ? e.message : e));
    txLog('TRANSMIT FAILED after ' + s + 's: ' + (e && e.message ? e.message : e));
  } finally {
    clearInterval(ticker);
    queryInFlight = false;
    btn.disabled = false;
    btn.innerHTML = "<span>⚡ TRANSMIT</span>";
    try { fetchStatus(); } catch(e) {}
  }
}

function renderSources(sources) {
  const sourcesCont = document.getElementById('sources-tags-container');
  if (!sourcesCont) return;
  sourcesCont.innerHTML = "";
  if (sources && sources.length > 0) {
    sources.forEach(s => {
      const span = document.createElement('span');
      span.className = "source-tag";
      span.innerText = "[[" + s.title + "]]";
      span.title = "Click to inspect note";
      span.onclick = () => viewStudy(s.path.split('/').pop());
      sourcesCont.appendChild(span);
    });
  } else {
    sourcesCont.innerHTML = "<span style='font-size:0.75rem; color:#94a3b8;'>No exact keyword matches found</span>";
  }
}

function jsonStringify(obj) {
  return JSON.stringify(obj);
}

async function exportQueryToObsidian() {
  if (!currentQueryResult) return;
  const title = prompt("Enter title for new Obsidian Note:", "Synthesis_" + currentQueryResult.query.slice(0, 25).replace(/[^a-zA-Z0-9]/g, '_'));
  if (!title) return;

  try {
    const res = await fetch('/api/save_note', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        title: title,
        content: `### Query:\n> ${currentQueryResult.query}\n\n### Synthesis:\n${currentQueryResult.answer}\n\n### Referenced Vault Sources:\n` + currentQueryResult.sources.map(s => `- [[${s.title}]]`).join('\n')
      })
    });
    const data = await res.json();
    alert("Saved note to Obsidian: " + data.title + ".md");
    fetchStatus();
  } catch(e) {
    alert("Error saving note: " + e);
  }
}

async function triggerReindex() {
  const btn = document.getElementById('btn-reindex');
  btn.innerText = "INDEXING...";
  btn.disabled = true;
  try {
    const res = await fetch('/api/reindex', { method: 'POST' });
    const data = await res.json();
    document.getElementById('val-notes').innerText = data.count.toLocaleString();
    btn.innerText = "✓ " + data.count + " NOTES";
    setTimeout(() => { btn.innerText = "🔄 RE-INDEX"; btn.disabled = false; }, 3000);
    fetchStatus();
  } catch(e) {
    btn.innerText = "ERROR";
    btn.disabled = false;
  }
}

function highlightPipeline(step) {
  const nodes = ['node-scout', 'node-index', 'node-drone', 'node-adversary', 'node-queen'];
  nodes.forEach(n => document.getElementById(n).classList.remove('active'));

  const s = step.toLowerCase();
  if (s.includes('scout') || s.includes('scanning')) document.getElementById('node-scout').classList.add('active');
  if (s.includes('index')) document.getElementById('node-index').classList.add('active');
  if (s.includes('drone') || s.includes('assimilating')) document.getElementById('node-drone').classList.add('active');
  if (s.includes('adversary')) document.getElementById('node-adversary').classList.add('active');
  if (s.includes('queen')) document.getElementById('node-queen').classList.add('active');
}

async function triggerAssimilation() {
  try {
    const btn = document.getElementById('btn-assimilate');
    btn.disabled = true;
    document.getElementById('btn-label').innerText = "DISPATCHING SWARM...";
    await fetch('/api/assimilate', { method: 'POST' });
    setTimeout(refreshStudies, 2000);
  } catch(e) {
    console.error(e);
  }
}

async function refreshStudies() {
  try {
    const res = await fetch('/api/studies');
    const studies = await res.json();
    const list = document.getElementById('studies-list');
    list.innerHTML = "";
    if (studies.length === 0) {
      list.innerHTML = '<div style="color: var(--text-dim); text-align: center; padding: 20px;">No studies currently in inbox. Drop daily papers into /inbox and click ASSIMILATE.</div>';
      return;
    }
    studies.forEach(s => {
      const card = document.createElement('div');
      card.className = "study-card";
      card.onclick = () => viewStudy(s.name);
      card.innerHTML = `
        <div class="study-card-header">
          <div class="study-title">📄 ${s.title}</div>
          <div class="study-badge ${s.has_hive_fabric ? 'badge-assimilated' : 'badge-pending'}">
            ${s.has_hive_fabric ? 'ASSIMILATED' : 'PENDING'}
          </div>
        </div>
        <div class="study-preview">${s.preview}...</div>
      `;
      list.appendChild(card);
    });
  } catch(e) {
    console.error(e);
  }
}

async function viewStudy(name) {
  try {
    const res = await fetch(`/api/study?name=${encodeURIComponent(name)}`);
    const content = await res.text();
    document.getElementById('modal-title').innerText = "📄 " + name;
    document.getElementById('modal-body').innerText = content;
    document.getElementById('note-modal').style.display = "flex";
  } catch(e) {
    console.error(e);
  }
}

function closeModal(event) {
  document.getElementById('note-modal').style.display = "none";
}

fetchStatus();
refreshStudies();
setInterval(fetchStatus, 1500);
setInterval(refreshStudies, 6000);
// v2.5: opening the dashboard auto-pins the selected core HOT in background.
setTimeout(() => {
  if (!autoPreloadFired) {
    autoPreloadFired = true;
    preloadModel(true);
  }
}, 2500);

window.executeHiveQuery = executeHiveQuery;
window.preloadModel = preloadModel;
window.onModelChange = onModelChange;
window.triggerReindex = triggerReindex;
window.triggerAssimilation = triggerAssimilation;
window.refreshStudies = refreshStudies;
window.closeModal = closeModal;
window.exportQueryToObsidian = exportQueryToObsidian;
window.viewStudy = viewStudy;

function wireQueenControls() {
  const btn = document.getElementById('btn-queen-query');
  const input = document.getElementById('queen-prompt-input');
  if (btn && !btn.dataset.wired) {
    btn.dataset.wired = '1';
    btn.setAttribute('type', 'button');
    btn.addEventListener('click', function(ev) {
      ev.preventDefault();
      executeHiveQuery();
    });
  }
  if (input && !input.dataset.wired) {
    input.dataset.wired = '1';
    input.addEventListener('keydown', function(ev) {
      if (ev.key === 'Enter') {
        ev.preventDefault();
        executeHiveQuery();
      }
    });
  }
}
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', wireQueenControls);
} else {
  wireQueenControls();
}
