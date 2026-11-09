// The run page: submits a question, then renders the run purely from its event stream,
// so a reload (or a shared ?run= link) replays the same run the same way.

const $ = id => document.getElementById(id);
const node = (tag, cls, text) => {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  if (text != null) el.textContent = text;
  return el;
};

const EXAMPLES = [
  ['Should a city replace sodium street lights with LED fixtures?', 'energy, cost and health guidance'],
  ['Is intermittent fasting more effective than calorie restriction for weight loss?', 'high impact: stops for approval'],
  ['Should a small team adopt Rust instead of Go for a new backend service?', 'engineering trade-offs'],
];
const STAGE_OF_STATE = { queued: null, planning: 'researching', researching: 'researching', analyzing: 'analyzing',
  critiquing: 'critiquing', awaiting_approval: 'critiquing', reporting: 'reporting' };
const STAGES = ['researching', 'analyzing', 'critiquing', 'reporting'];
const TERMINAL = new Set(['done', 'abstained', 'rejected', 'failed']);
const ORIGIN_WORD = { web: 'Web', wikipedia: 'Wikipedia', arxiv: 'arXiv', upload: 'Your document', memory: 'Earlier run' };

const storage = {
  get(key) { try { return localStorage.getItem(key); } catch { return null; } },
  set(key, value) { try { localStorage.setItem(key, value); } catch { /* private mode: keep it in memory only */ } },
};
const sessionId = storage.get('assay.session') || (() => {
  const id = 'web-' + crypto.getRandomValues(new Uint32Array(2)).join('').slice(0, 12);
  storage.set('assay.session', id);
  return id;
})();
const apiKey = new URLSearchParams(location.search).get('key') || storage.get('assay.key');
if (apiKey) storage.set('assay.key', apiKey);

let run = null;
let stream = null;

function freshRun(id, question) {
  return { id, question, sources: new Map(), chunks: new Map(), claims: new Map(), spans: [],
    stageStart: {}, stageEnd: {}, state: null, selected: null, report: null };
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (apiKey) headers['x-api-key'] = apiKey;
  const response = await fetch(path, { ...options, headers });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = typeof body.detail === 'string' ? body.detail
      : Array.isArray(body.detail) ? body.detail.map(d => `${(d.loc || []).slice(-1)[0]}: ${d.msg}`).join('; ')
      : `HTTP ${response.status}`;
    const err = new Error(response.status === 429 ? `Busy: ${detail}. Try again in a few seconds.`
      : response.status === 401 ? 'This server needs an API key: open the page with ?key=YOUR_KEY once.' : detail);
    err.status = response.status;
    throw err;
  }
  return body;
}

function say(text, isError = false) {
  const status = $('status');
  status.replaceChildren(node('span', isError ? 'err' : '', text));
}

/* ---------- starting and loading runs ---------- */

async function startRun(question, impact) {
  $('go').disabled = true;
  try {
    const body = { question, session_id: sessionId };
    if (impact) body.impact = impact;
    const { run_id } = await api('/research', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) });
    history.pushState({}, '', '?run=' + run_id);
    openRun(run_id, question);
  } catch (err) {
    say(err.message, true);
  } finally {
    $('go').disabled = false;
  }
}

function openRun(runId, question = '') {
  if (stream) stream.close();
  run = freshRun(runId, question);
  resetView();
  stream = new EventSource(`/runs/${runId}/events`);
  for (const type of ['run.state', 'query.planned', 'source.found', 'chunk.ranked', 'claim.drafted', 'claim.verdict', 'report.ready', 'span']) {
    stream.addEventListener(type, frame => {
      const envelope = JSON.parse(frame.data);
      if (!run || envelope.run_id !== run.id) return;
      HANDLERS[type](envelope.data, envelope);
    });
  }
  stream.onerror = () => {
    if (run && TERMINAL.has(run.state)) { stream.close(); return; }
    say('Connection dropped, reconnecting...');
  };
  if (!question) api(`/runs/${runId}`).then(view => { run.question = view.question; $('questionEcho').textContent = view.question; $('question').value = view.question; })
    .catch(err => say(err.status === 404 ? 'That run does not exist on this server.' : err.message, true));
}

function resetView() {
  $('empty').hidden = true;
  $('live').hidden = false;
  $('questionEcho').textContent = run.question;
  $('approval').hidden = true;
  $('decision').className = 'decision';
  $('verdict').textContent = 'Researching...';
  $('verdict').className = 'pending';
  $('recommendation').textContent = '';
  $('heldCount').textContent = '-';
  $('heldOf').textContent = '';
  $('track').replaceChildren();
  $('claims').replaceChildren();
  $('claimsNote').textContent = 'Drafted claims appear here and are checked one by one.';
  $('extras').replaceChildren();
  $('scores').hidden = true;
  $('sources').replaceChildren();
  $('sourceCount').textContent = 'reliability';
  $('evidenceFor').textContent = 'select a claim';
  showEvidenceHint();
  $('traceBtn').disabled = false;
  for (const step of document.querySelectorAll('.step')) { step.classList.remove('active', 'done'); step.querySelector('.t').textContent = ''; }
  say('Queued.');
}

/* ---------- event handlers ---------- */

const HANDLERS = {
  'run.state'(data) {
    run.state = data.state;
    const stage = STAGE_OF_STATE[data.state];
    for (const s of STAGES) {
      if (run.stageStart[s] != null && run.stageEnd[s] == null && s !== stage) run.stageEnd[s] = data.elapsed_ms;
    }
    if (stage && run.stageStart[stage] == null) run.stageStart[stage] = data.elapsed_ms;
    if (TERMINAL.has(data.state)) for (const s of STAGES) if (run.stageStart[s] != null && run.stageEnd[s] == null) run.stageEnd[s] = data.elapsed_ms;
    paintFlow(stage, data.state);

    $('approval').hidden = data.state !== 'awaiting_approval';
    if (data.state === 'awaiting_approval') {
      $('approvalWhy').textContent = (data.detail?.reasons || []).join('; ');
      say('Held for approval.');
      showDraft();
    } else if (data.state === 'failed') {
      $('decision').className = 'decision failed';
      $('verdict').textContent = 'The run failed.';
      $('verdict').className = '';
      $('recommendation').textContent = data.detail?.reason || '';
      say('Failed: ' + (data.detail?.reason || 'unknown reason'), true);
    } else if (data.state === 'rejected') {
      $('decision').className = 'decision rejected';
      $('verdict').textContent = 'Rejected at review.';
      $('verdict').className = '';
      $('recommendation').textContent = data.detail?.note || '';
      say('Rejected by reviewer.');
    } else if (TERMINAL.has(data.state)) {
      say(`Finished in ${(data.elapsed_ms / 1000).toFixed(1)} s.`);
    } else {
      say({ planning: 'Planning searches...', researching: 'Gathering sources...', analyzing: 'Reading passages and drafting claims...',
        critiquing: 'Checking claims against their passages...', reporting: 'Writing the report...', queued: 'Queued.' }[data.state] || data.state);
    }
    if (TERMINAL.has(data.state)) loadEvaluation();
  },

  'query.planned'(data) {
    say(`Searching ${data.tool_calls.length} ways` + (data.reused_findings ? `, reusing ${data.reused_findings} earlier findings` : '') + '...');
  },

  'source.found'(data) {
    run.sources.set(data.source_id, data);
    const row = node('div', 'srcrow');
    const who = node('div');
    const link = node('a', null, data.title || data.domain);
    if (/^https?:/.test(data.url)) { link.href = data.url; link.target = '_blank'; link.rel = 'noopener'; }
    link.title = data.url;
    who.append(link, node('span', 'kind', `${data.domain} · ${ORIGIN_WORD[data.origin] || data.origin}`));
    const gauge = node('div', 'gauge');
    const level = node('i');
    level.style.width = Math.round(data.reliability * 100) + '%';
    gauge.append(level);
    row.append(node('span', 'n', data.source_id), who, gauge, node('span', 'v', data.reliability.toFixed(2)));
    $('sources').append(row);
    $('sourceCount').textContent = `${run.sources.size} found · reliability`;
  },

  'chunk.ranked'(data) { run.chunks.set(data.chunk_id, data); },

  'claim.drafted'(data) {
    const claim = { ...data, verdict: null };
    run.claims.set(data.id, claim);
    const row = node('div', 'claim');
    row.dataset.id = data.id;
    row.tabIndex = 0;
    claim.row = row;
    paintClaim(claim);
    row.addEventListener('click', () => selectClaim(data.id));
    row.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); selectClaim(data.id); } });
    $('claims').append(row);
    paintMeter();
    if (!run.selected) selectClaim(data.id);
  },

  'claim.verdict'(data) {
    const claim = run.claims.get(data.id);
    if (!claim) return;
    claim.verdict = data;
    paintClaim(claim);
    paintMeter();
    // the first removal is the most informative thing on the page, so the evidence panel follows it
    if (data.struck && !run.followedStrike) { run.followedStrike = true; selectClaim(data.id); }
    else if (run.selected === data.id) selectClaim(data.id);
  },

  'report.ready'(data) {
    run.report = data.report;
    paintReport(data.report);
  },

  span(data) {
    run.spans.push(data);
    if ($('trace').open) paintTrace();
  },
};

/* ---------- painting ---------- */

function paintFlow(stage, state) {
  const reached = stage ? STAGES.indexOf(stage) : TERMINAL.has(state) ? STAGES.length : -1;
  document.querySelectorAll('.step').forEach((step, i) => {
    const s = step.dataset.stage;
    const finished = i < reached || (TERMINAL.has(state) && run.stageStart[s] != null);
    step.classList.toggle('done', finished);
    step.classList.toggle('active', !finished && i === reached);
    const start = run.stageStart[s], end = run.stageEnd[s];
    step.querySelector('.t').textContent = start != null && end != null ? ((end - start) / 1000).toFixed(1) + ' s' : '';
  });
}

function sourceIdsOf(claim) {
  const ids = [];
  for (const chunkId of claim.chunk_ids) {
    const sid = run.chunks.get(chunkId)?.source_id ?? Number((/^s(\d+):/.exec(chunkId) || [])[1]);
    if (sid && !ids.includes(sid)) ids.push(sid);
  }
  return ids;
}

function paintClaim(claim) {
  const v = claim.verdict;
  const state = !v ? 'checking' : v.struck ? 'x' : 's';
  claim.row.className = 'claim' + (state === 'x' ? ' x' : '') + (run.selected === claim.id ? ' sel' : '');
  const word = !v ? 'Checking' : !v.struck ? 'Verified' : v.verdict === 'partial' ? 'Removed: partial' : 'Removed';
  const badge = node('span', 'badge ' + state, word);
  const text = node('div');
  text.append(node('span', 't', claim.text));
  if (v?.struck) text.append(node('span', 'why', explainStrike(v)));
  const cites = node('div', 'cites');
  for (const sid of sourceIdsOf(claim)) cites.append(node('i', null, sid));
  claim.row.replaceChildren(badge, text, cites);
}

function explainStrike(v) {
  if (!v.span_found && v.verdict === 'supported') return 'The quoted words were not found in the cited passage.';
  const reason = (v.reason || '').trim();
  return reason ? reason.charAt(0).toUpperCase() + reason.slice(1).replace(/\.?$/, '.') : 'No cited passage supports it.';
}

function paintMeter() {
  const claims = [...run.claims.values()];
  const held = claims.filter(c => c.verdict && !c.verdict.struck).length;
  const checked = claims.filter(c => c.verdict).length;
  $('heldCount').textContent = `${held} of ${claims.length}`;
  $('heldOf').textContent = 'held';
  $('track').replaceChildren(...claims.map(c => node('i', !c.verdict ? '' : c.verdict.struck ? 'x' : 's')));
  $('claimsNote').textContent = checked < claims.length ? `${claims.length - checked} still being checked`
    : `${held} verified, ${claims.length - held} removed`;
}

function paintReport(report) {
  const decision = $('decision');
  decision.className = 'decision ' + (report.abstained ? 'abstained' : 'held');
  $('verdict').className = '';
  // A failed model call is not thin evidence; the heading must not blame the sources for it.
  const capacity = /capacity ran out|could not draft/.test(report.abstain_reason || '');
  $('verdict').textContent = !report.abstained ? report.verdict
    : capacity ? 'No decision this time.' : 'Insufficient evidence to decide.';
  $('recommendation').textContent = report.abstained
    ? report.abstain_reason || 'Too few claims survived the check to support a decision, so assay declines to make one. The removed claims and their reasons are below.'
    : report.recommendation?.text || '';
  if (!report.abstained && report.verdict_check === 'passed') {
    $('recommendation').append(node('span', 'checked', 'Conclusion checked against the verified claims.'));
  }
  const extras = $('extras');
  extras.replaceChildren();
  const list = (title, items) => {
    if (!items.length) return;
    const section = node('section');
    const ul = node('ul');
    for (const item of items) ul.append(node('li', null, item));
    section.append(node('h4', null, title), ul);
    extras.append(section);
  };
  list('Risks', report.risks.map(r => r.text));
  list('Open questions', report.open_questions);
  if (report.reused_prior_findings.length) list('Reused from earlier runs', report.reused_prior_findings);
}

async function showDraft() {
  try {
    const view = await api(`/runs/${run.id}`);
    if (run.state !== 'awaiting_approval' || !view.draft) return;
    $('verdict').className = '';
    $('verdict').textContent = view.draft.recommendation || 'Draft ready for review.';
    $('recommendation').textContent = 'Draft held for review. Only the verified claims below will be used in the released report.';
  } catch (err) {
    say(err.message, true);
  }
}

async function loadEvaluation() {
  try {
    const view = await api(`/runs/${run.id}`);
    const e = view.evaluation;
    if (!e) return;
    const pct = x => x == null ? 'n/a' : x.toFixed(2);
    const tiles = [
      [pct(e.faithfulness), 'Faithfulness'],
      [pct(e.citation_precision), 'Citation precision'],
      [e.relevance_grade != null ? `${e.relevance_grade} / 5` : pct(e.relevance_similarity), 'Relevance'],
      [String(e.source_diversity), 'Distinct domains'],
    ];
    $('scores').replaceChildren(...tiles.map(([value, label]) => {
      const tile = node('div', 'score');
      tile.append(node('b', null, value), node('span', null, label));
      if (label === 'Relevance' && e.relevance_reason) tile.title = e.relevance_reason;
      return tile;
    }));
    $('scores').hidden = false;
    if (view.latency_ms != null) say(`Finished in ${(view.latency_ms / 1000).toFixed(1)} s of a ${view.deadline_s} s budget.`);
  } catch (err) {
    say(err.message, true);
  }
}

/* ---------- evidence panel ---------- */

function showEvidenceHint() {
  const hint = $('quote');
  hint.className = 'quote muted';
  hint.replaceChildren('Click a claim to see the passage it cites and whether the quoted words were found there.');
  for (const extra of document.querySelectorAll('.quote.cited')) extra.remove();
}

function selectClaim(id) {
  const claim = run.claims.get(id);
  if (!claim) return;
  run.selected = id;
  for (const c of run.claims.values()) c.row.classList.toggle('sel', c.id === id);
  $('evidenceFor').textContent = 'for ' + id;
  for (const extra of document.querySelectorAll('.quote.cited')) extra.remove();
  const anchor = $('quote');
  anchor.className = 'quote muted';
  const v = claim.verdict;
  anchor.replaceChildren(v ? verdictLine(v) : 'The Critic has not checked this claim yet.');
  if (v?.quote) anchor.className = 'quote';
  if (v?.quote) {
    const quoted = node('p');
    quoted.append('“', node('mark', v.struck ? 'x' : '', v.quote), '”');
    anchor.prepend(quoted);
  }
  let after = anchor;
  const place = card => { after.after(card); after = card; };
  // The passage the verdict rests on: the matched sentence in its own context.
  if (v?.passage) {
    const chunk = run.chunks.get(v.passage.chunk_id);
    const card = node('div', 'quote cited');
    card.append(sourceHead(chunk?.source_id, v.passage.chunk_id));
    const p = node('p');
    p.append(v.passage.before, node('mark', v.struck ? 'x' : '', v.passage.match), v.passage.after);
    card.append(p);
    const link = sourceLink(chunk?.source_id);
    if (link) card.append(link);
    place(card);
  }
  const others = claim.chunk_ids.filter(id => id !== v?.passage?.chunk_id && run.chunks.has(id));
  if (others.length) {
    const card = node('div', 'quote cited');
    card.append(node('div', 'check', v?.passage ? 'Also cited' : 'Cited passages'));
    for (const chunkId of others) {
      const row = sourceHead(run.chunks.get(chunkId).source_id, chunkId);
      const link = sourceLink(run.chunks.get(chunkId).source_id);
      card.append(row);
      if (link && !v?.passage) card.append(link);
    }
    place(card);
  }
}

function sourceHead(sourceId, chunkId) {
  const source = run.sources.get(sourceId);
  const head = node('div', 'src');
  head.append(node('b', null, `[${sourceId ?? '?'}] ${source?.title || source?.domain || 'source'}`),
    node('span', null, `${chunkId}${source ? ' · ' + source.reliability.toFixed(2) : ''}`));
  return head;
}

function sourceLink(sourceId) {
  const source = run.sources.get(sourceId);
  if (!source || !/^https?:/.test(source.url)) return null;
  const link = node('a', null, 'Open source');
  link.href = source.url; link.target = '_blank'; link.rel = 'noopener';
  return link;
}

function verdictLine(v) {
  const line = node('div', 'check');
  const label = !v.struck ? 'Found in the cited passage.' : v.span_found ? 'Found, but the Critic judged it does not support the claim.' : 'Not found in any cited passage.';
  line.append(node('b', v.struck ? 'x' : 's', v.struck ? 'Removed' : 'Verified'), label);
  if (v.reason) line.append(node('div', null, v.reason));
  return line;
}

/* ---------- trace ---------- */

function paintTrace() {
  const rows = [...run.spans].sort((a, b) => a.start.localeCompare(b.start)).map(s => {
    const tr = node('tr');
    const who = s.kind === 'llm' ? (s.model || '') + (s.attrs?.deployment ? ` (${s.attrs.deployment})` : '')
      : s.kind === 'tool' ? JSON.stringify(s.attrs?.arguments || {}).slice(0, 60) : '';
    const statusCell = node('td', s.status === 'ok' ? '' : 'bad', s.status + (s.error ? `: ${s.error}` : ''));
    const whoCell = node('td', s.fallback_used ? 'fb' : '', who + (s.fallback_used ? '  fallback' : '') + (s.retries ? `  ${s.retries} retries` : ''));
    tr.append(node('td', null, s.name), node('td', null, s.kind), whoCell,
      node('td', 'r', `${Math.round(s.latency_ms)} ms`),
      node('td', 'r', s.tokens_in || s.tokens_out ? `${s.tokens_in ?? 0} / ${s.tokens_out ?? 0}` : ''), statusCell);
    return tr;
  });
  $('spans').replaceChildren(...rows);
  const llm = run.spans.filter(s => s.kind === 'llm');
  const tokens = llm.reduce((n, s) => n + (s.tokens_in || 0) + (s.tokens_out || 0), 0);
  const failures = run.spans.filter(s => s.status !== 'ok').length;
  const fallbacks = llm.filter(s => s.fallback_used).length;
  $('traceSummary').textContent = `${run.spans.length} spans · ${llm.length} model calls · ${tokens} tokens · ${fallbacks} fallbacks · ${failures} failures`;
}

/* ---------- wiring ---------- */

for (const [question, note] of EXAMPLES) {
  const button = node('button');
  button.type = 'button';
  button.append(question, node('small', null, note));
  button.addEventListener('click', () => { $('question').value = question; startRun(question, ''); });
  $('examples').append(button);
}

$('ask').addEventListener('submit', e => {
  e.preventDefault();
  const question = $('question').value.trim();
  if (question.length < 8) { say('A question needs at least 8 characters.', true); return; }
  startRun(question, $('impact').value);
});

async function decide(decision) {
  for (const id of ['approve', 'reject']) $(id).disabled = true;
  try {
    await api(`/runs/${run.id}/approval`, { method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ decision, note: $('approvalNote').value.trim() }) });
  } catch (err) {
    say(err.message, true);
  } finally {
    for (const id of ['approve', 'reject']) $(id).disabled = false;
  }
}
$('approve').addEventListener('click', () => decide('approve'));
$('reject').addEventListener('click', () => decide('reject'));

$('upload').addEventListener('change', async e => {
  const file = e.target.files[0];
  e.target.value = '';
  if (!file) return;
  if (file.size > 20 * 1024 * 1024) { say(`${file.name} is larger than the 20 MB upload limit.`, true); return; }
  say(`Uploading ${file.name}...`);
  const form = new FormData();
  form.append('file', file);
  form.append('session_id', sessionId);
  try {
    const doc = await api('/documents', { method: 'POST', body: form });
    say(`Added ${doc.name} (${doc.chars.toLocaleString()} characters). Your next question in this session will search it.`);
  } catch (err) {
    say(err.message, true);
  }
});

$('traceBtn').addEventListener('click', () => { paintTrace(); $('trace').showModal(); });
$('traceClose').addEventListener('click', () => $('trace').close());

window.addEventListener('popstate', () => {
  const id = new URLSearchParams(location.search).get('run');
  if (id) openRun(id);
});
const initial = new URLSearchParams(location.search).get('run');
if (initial && /^[0-9a-f]{12}$/.test(initial)) openRun(initial);
