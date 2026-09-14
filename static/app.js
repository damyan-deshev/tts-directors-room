const $ = selector => document.querySelector(selector);
const editor = $('#editor');
const i18n = window.DirectorsRoomI18n;
const t = (key, values = {}) => i18n.t(key, values);
const state = {
  data: null,
  activeRegionId: null,
  selectedTakeId: null,
  selection: {start: 0, end: 0},
  rendering: false,
  saveTimer: null,
  busy: false,
  latestRender: null,
  tagsDimmed: false,
  ab: {regionId: null, a: null, b: null, playing: 'a'},
  monitor: {muted: new Set(), solo: new Set()},
  timelineZoom: 90,
  waveformCache: new Map(),
  waveformDurations: new Map(),
  playhead: {regionId: null, takeId: null, sourceMs: 0},
  transport: null,
  editQueue: Promise.resolve(),
  baselinePoller: null,
  baselineNotice: null,
  locale: i18n.getLocale(),
};
const transportAudio = new Audio();
const tagPattern = /<\|(?:emotion|style|prosody):[a-z_]+\|>/g;
const emotionLabels = {
  elation:'възторг', amusement:'усмивка', enthusiasm:'ентусиазъм', determination:'решимост',
  pride:'гордост', contentment:'удовлетворение', affection:'привързаност', relief:'облекчение',
  contemplation:'съзерцание', confusion:'объркване', surprise:'изненада', awe:'благоговение',
  longing:'копнеж', arousal:'възбуда', anger:'гняв', fear:'страх', disgust:'отвращение',
  bitterness:'горчивина', sadness:'тъга', shame:'срам', helplessness:'безпомощност',
};

async function api(path, payload) {
  const options = payload === undefined ? {} : {
    method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify(payload),
  };
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

function toast(message, bad = false) {
  const node = $('#toast');
  node.textContent = message;
  node.className = `toast show${bad ? ' bad' : ''}`;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { node.className = 'toast'; }, 3400);
}

function escapeHtml(value) {
  return value.replace(/[&<>"']/g, char => ({
    '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#039;',
  })[char]);
}

function activeRegion() {
  return state.data.project.regions.find(region => region.id === state.activeRegionId)
    || state.data.project.regions[0];
}

function activeTake(region = activeRegion()) {
  return region?.takes.find(take => take.id === (state.selectedTakeId || region.active_take_id))
    || region?.takes.find(take => take.id === region.active_take_id)
    || region?.takes.at(-1);
}

function selectedTake(region = activeRegion()) {
  return region?.takes.find(take => take.id === state.selectedTakeId)
    || region?.takes.find(take => take.id === region.active_take_id)
    || region?.takes.at(-1);
}

function edlTake(region = activeRegion()) {
  return region?.takes.find(take => take.id === region.active_take_id) || null;
}

function takeUrl(take) { return take?.audio_url || `/${take.audio_file}`; }
function regionIndex() { return state.data.project.regions.findIndex(region => region.id === state.activeRegionId); }
function effectiveTrack(region) { return state.data.project.tracks.find(track => track.id === region.track_id); }
function effectiveVoice(region) { return region.overrides.voice_mode || effectiveTrack(region).voice_mode; }
function effectiveControl(region, key) {
  const local = region.overrides.controls[key];
  return local === null || local === undefined ? effectiveTrack(region).delivery_defaults[key] : local;
}

function addOption(select, value, label) { select.add(new Option(label, value)); }
function fillSelect(select, values, current, natural = state.locale === 'bg' ? 'Естествено · без token' : 'Natural · no token') {
  select.innerHTML = '';
  addOption(select, '', natural);
  values.forEach(value => addOption(
    select, value, state.locale === 'bg' && emotionLabels[value] ? `${value} · ${emotionLabels[value]}` : value,
  ));
  select.value = current || '';
}

function trackLabel(track) {
  const known = {
    narrator: {en: 'Narrator', bg: 'Разказвач'},
    dimitar: {en: 'Dimitar', bg: 'Димитър'},
    monk: {en: 'Monk', bg: 'Монах'},
    other: {en: 'Other voice', bg: 'Друг глас'},
  }[track.id];
  if (!known || ![known.en, known.bg].includes(track.label)) return track.label;
  return known[state.locale];
}

function updateLocaleSwitch() {
  document.querySelectorAll('[data-locale]').forEach(button => {
    const active = button.dataset.locale === state.locale;
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', String(active));
  });
}

function setUiLocale(locale) {
  state.locale = i18n.setLocale(locale);
  updateLocaleSwitch();
  if (state.data) renderAll(false);
}

function renderEngine() {
  const engine = state.data.engine;
  $('#engineDot').className = `dot ${engine.reachable ? 'ok' : 'bad'}`;
  $('#engineLabel').textContent = engine.reachable
    ? t(engine.model_loaded ? 'engine.loaded' : 'engine.lazy')
    : t('engine.unreachable');
  $('#engineEndpoint').textContent = engine.endpoint;
  $('#contextCapability').className = 'capability-warning ok';
  $('#contextCapability').textContent = t('context.boundary');
}

function renderSummary() {
  const summary = state.data.summary;
  $('#summary').innerHTML = `<span>${t('summary.regions', {count: summary.regions})}</span><span>${t('summary.active', {count: summary.active})}</span><span>${t('summary.approved', {count: summary.approved})}</span>`;
}

function minutesLabel(seconds) {
  const minutes = Math.max(1, Math.round(Number(seconds || 0) / 60));
  return t('time.minutes', {count: minutes});
}

function baselineIsActive(activity) {
  return ['queued', 'running', 'cancelling'].includes(activity?.state);
}

function renderBaseline() {
  const baseline = state.data.baseline || {};
  const plan = baseline.plan || {};
  const activity = baseline.activity || {state: 'idle'};
  const active = baselineIsActive(activity);
  const ready = activity.state === 'completed' && !plan.target_regions && !state.data.workflow?.render_outdated;
  const entry = $('#baselineEntry');
  entry.hidden = !plan.target_regions && !active && !ready;
  entry.className = `baseline-entry${active ? ' running' : ready ? ' ready' : ''}`;
  entry.textContent = active
    ? t('baseline.entry.running', {done: activity.processed_regions || 0, total: activity.target_regions || 0})
    : ready ? t('baseline.entry.ready') : t('baseline.entry');

  const select = $('#baselineVoice');
  const previous = select.value;
  const fallback = state.data.project.tracks[0]?.voice_mode || state.data.voices[0]?.id || '';
  select.innerHTML = '';
  state.data.voices.forEach(voice => addOption(select, voice.id, voice.label));
  select.value = state.data.voices.some(voice => voice.id === previous) ? previous : fallback;
  select.disabled = active;

  $('#baselinePlan').innerHTML = plan.target_regions
    ? `<strong>${t('baseline.plan.scope', {active: plan.existing_active_regions, missing: plan.target_regions})}</strong><span>${t('baseline.plan.estimate', {lower: minutesLabel(plan.estimated_lower_seconds), upper: minutesLabel(plan.estimated_upper_seconds), confidence: plan.confidence, samples: plan.timing_sample_count})}</span><span>${t(plan.model_loaded ? 'baseline.plan.loaded' : 'baseline.plan.unreported')} · ${escapeHtml(plan.model || 'model unreported')}</span>`
    : `<strong>${t('baseline.plan.complete')}</strong><span>${t('baseline.plan.render')}</span>`;

  const job = $('#baselineJob');
  job.hidden = activity.state === 'idle';
  job.className = `baseline-job ${activity.state || 'idle'}`;
  const total = activity.target_regions || 0;
  const processed = activity.processed_regions || 0;
  const titleKey = active ? 'baseline.job.running' : activity.state === 'completed' ? 'baseline.job.completed' : activity.state === 'cancelled' ? 'baseline.job.cancelled' : activity.state === 'failed' ? 'baseline.job.failed' : 'baseline.job.previous';
  const knownMessageKey = ['running', 'cancelling', 'completed', 'cancelled'].includes(activity.state)
    ? `baseline.job.${activity.state}.message` : null;
  const message = knownMessageKey ? t(knownMessageKey) : activity.message || '';
  job.innerHTML = `<strong>${t(titleKey, {done: processed, total})}</strong><span>${escapeHtml(message)}</span>${activity.current_region_id ? `<span>${t('baseline.job.current', {region: escapeHtml(activity.current_region_id)})}</span>` : ''}`;
  $('#startBaseline').hidden = active || !plan.target_regions;
  $('#startBaseline').disabled = !plan.target_regions || !state.data.engine?.reachable;
  $('#cancelBaseline').hidden = !active;
  ensureBaselinePolling();
}

function ensureBaselinePolling() {
  const activity = state.data?.baseline?.activity || {};
  if (!baselineIsActive(activity)) {
    if (state.baselinePoller) clearTimeout(state.baselinePoller);
    state.baselinePoller = null;
    return;
  }
  if (state.baselinePoller) return;
  state.baselinePoller = setTimeout(async () => {
    state.baselinePoller = null;
    try {
      state.data = await api('/api/director/state');
      const current = state.data.baseline?.activity || {};
      renderAll(false);
      if (!baselineIsActive(current)) {
        const notice = `${current.job_id}:${current.state}`;
        if (state.baselineNotice !== notice) {
          state.baselineNotice = notice;
          toast(
            current.state === 'completed'
              ? t('baseline.complete.toast')
              : current.message || t('baseline.finished.toast'),
            current.state === 'failed',
          );
        }
      }
    } catch (error) {
      toast(t('baseline.poll.error', {error: error.message}), true);
    }
    ensureBaselinePolling();
  }, 1000);
}

function renderWorkflow() {
  const workflow = state.data.workflow || {};
  const history = state.data.history || {};
  const generate = $('#generateSignal'), qa = $('#qaSignal'), render = $('#renderSignal');
  generate.textContent = t('workflow.generate', {count: workflow.generation_required || 0});
  generate.className = `workflow-signal generate${workflow.generation_required ? ' lit' : ''}`;
  qa.textContent = t('workflow.qa', {count: workflow.qa_required || 0});
  qa.className = `workflow-signal qa${workflow.qa_required ? ' lit' : ''}`;
  render.textContent = t(workflow.render_outdated ? 'workflow.render.outdated' : 'workflow.render.current');
  render.className = `workflow-signal render${workflow.render_outdated ? ' lit' : ''}`;
  const matchingRender = workflow.last_render_id
    ? state.data.project.renders.find(item => item.id === workflow.last_render_id)
    : null;
  state.latestRender = matchingRender || null;
  if (matchingRender) {
    $('#renderAudio').src = matchingRender.audio_url;
    $('#renderAudio').hidden = false;
    $('#renderMeta').textContent = t('render.cached', {seconds: matchingRender.duration_seconds, hash: matchingRender.edl_sha256.slice(0, 10)});
  } else {
    $('#renderAudio').pause(); $('#renderAudio').hidden = true;
  }
  $('#undoProject').disabled = !history.can_undo;
  $('#redoProject').disabled = !history.can_redo;
  $('#undoProject').title = history.can_undo ? t('history.undo', {action: history.undo_action}) : t('history.undo.none');
  $('#redoProject').title = history.can_redo ? t('history.redo', {action: history.redo_action}) : t('history.redo.none');
  $('#historyDepth').textContent = t('state.history', {cursor: history.cursor || 0, depth: history.depth || 0});
  const warning = $('#stateWarning');
  warning.textContent = state.data.state_warning || '';
  warning.hidden = !state.data.state_warning;
  renderGenerationActivity();
  renderBaseline();
}

function renderGenerationActivity() {
  const activity = state.data.generation_activity || {state: 'idle'};
  const node = $('#operationStatus');
  node.hidden = activity.state === 'idle';
  node.className = `operation-status ${activity.state}`;
  node.textContent = activity.state === 'running'
    ? t('generation.running', {region: activity.region_id, operation: activity.operation, stage: activity.stage})
    : activity.state === 'failed'
      ? t('generation.failed', {region: activity.region_id || '?', message: activity.message})
      : t('generation.complete', {region: activity.region_id, take: activity.take_id || ''});
}

function monitorAudible(trackId) {
  return state.monitor.solo.size ? state.monitor.solo.has(trackId) : !state.monitor.muted.has(trackId);
}

function canAudition(region, announce = true) {
  const allowed = monitorAudible(region.track_id);
  if (!allowed && announce) toast(t('monitor.muted.toast', {track: trackLabel(effectiveTrack(region))}), true);
  return allowed;
}

function renderMonitoring() {
  const active = state.monitor.muted.size || state.monitor.solo.size;
  $('#monitorBanner').hidden = !active;
  $('#monitorSummary').textContent = t('monitor.summary', {muted: [...state.monitor.muted].join(', ') || '—', solo: [...state.monitor.solo].join(', ') || '—'});
}

function toggleMonitor(trackId, kind) {
  const mine = state.monitor[kind], other = state.monitor[kind === 'muted' ? 'solo' : 'muted'];
  mine.has(trackId) ? mine.delete(trackId) : mine.add(trackId);
  other.delete(trackId);
  transportAudio.pause(); document.querySelectorAll('audio').forEach(audio => audio.pause());
  renderTrackStack(); renderMonitoring();
}

function timelineTake(region) {
  if (region.id === state.activeRegionId && state.selectedTakeId) {
    return directorTake(region, state.selectedTakeId) || edlTake(region);
  }
  return edlTake(region);
}

function timelineGapWidth(region) {
  return Math.max(8, Number(region.pause_after_ms || 0) / 1000 * state.timelineZoom / 4);
}

function regionWidth(region) {
  const take = timelineTake(region);
  const seconds = Number(take?.duration_seconds) || Math.max(1.8, region.source_text.replace(tagPattern, '').length / 15);
  return Math.max(110, Math.min(3200, seconds * state.timelineZoom / 4));
}

async function paintWaveform(canvas, region, take) {
  if (!take) return;
  const key = `${take.audio_sha256}:500`;
  try {
    if (!state.waveformCache.has(key)) {
      state.waveformCache.set(key, api(`/api/director/waveform?region_id=${encodeURIComponent(region.id)}&take_id=${encodeURIComponent(take.id)}&buckets=500`));
    }
    const payload = await state.waveformCache.get(key);
    if (!canvas.isConnected) return;
    state.waveformDurations.set(`${region.id}:${take.id}`, payload.duration_ms);
    canvas.closest('.timeline-clip').dataset.durationMs = payload.duration_ms;
    const ratio = window.devicePixelRatio || 1, width = Math.max(1, canvas.clientWidth), height = Math.max(1, canvas.clientHeight);
    canvas.width = Math.round(width * ratio); canvas.height = Math.round(height * ratio);
    const ctx = canvas.getContext('2d'); ctx.scale(ratio, ratio); ctx.clearRect(0, 0, width, height);
    ctx.strokeStyle = getComputedStyle(canvas).color; ctx.lineWidth = 1; ctx.beginPath();
    payload.peaks.forEach((peak, index) => {
      const x = index * width / Math.max(1, payload.peaks.length - 1), amplitude = peak * height * .46;
      ctx.moveTo(x, height / 2 - amplitude); ctx.lineTo(x, height / 2 + amplitude);
    });
    ctx.stroke();
    const edits = take.edits || {}, duration = Math.max(1, payload.duration_ms);
    const xAt = milliseconds => Math.max(0, Math.min(width, milliseconds / duration * width));
    ctx.fillStyle = 'rgba(90,65,45,.20)';
    const trimLeft = xAt(edits.trim_start_ms || 0), trimRight = xAt(duration - (edits.trim_end_ms || 0));
    if (trimLeft) ctx.fillRect(0, 0, trimLeft, height);
    if (trimRight < width) ctx.fillRect(trimRight, 0, width - trimRight, height);
    ctx.strokeStyle = '#a66d2d'; ctx.lineWidth = 1.4;
    if (edits.fade_in_ms) { ctx.beginPath(); ctx.moveTo(trimLeft, height - 2); ctx.lineTo(xAt((edits.trim_start_ms || 0) + edits.fade_in_ms), 2); ctx.stroke(); }
    if (edits.fade_out_ms) { ctx.beginPath(); ctx.moveTo(xAt(duration - (edits.trim_end_ms || 0) - edits.fade_out_ms), 2); ctx.lineTo(trimRight, height - 2); ctx.stroke(); }
    const envelope = edits.volume_envelope || [];
    if (envelope.length) {
      ctx.strokeStyle = '#9c3f39'; ctx.beginPath();
      envelope.forEach((point, index) => {
        const x = xAt(point.at_ms), y = height / 2 - Math.max(-12, Math.min(12, point.gain_db)) / 24 * (height - 4);
        index ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
      });
      ctx.stroke();
    }
    ctx.strokeStyle = '#b98a2f';
    (edits.inserted_pauses || []).forEach(point => { const x=xAt(point.at_ms); ctx.beginPath(); ctx.moveTo(x,0); ctx.lineTo(x,height); ctx.stroke(); });
    updatePlayheads();
  } catch (_) { canvas.classList.add('waveform-error'); }
}

function sourceToEditedMs(edits, sourceMs) {
  const trimStart = Number(edits?.trim_start_ms || 0);
  const pauses = (edits?.inserted_pauses || []).slice().sort((a, b) => a.at_ms - b.at_ms);
  return Math.max(0, sourceMs - trimStart) + pauses
    .filter(point => Number(point.at_ms) <= sourceMs)
    .reduce((total, point) => total + Number(point.duration_ms || 0), 0);
}

function editedToSourceMs(edits, editedMs, durationMs = Infinity) {
  const trimStart = Number(edits?.trim_start_ms || 0), trimEnd = Number(edits?.trim_end_ms || 0);
  const pauses = (edits?.inserted_pauses || []).slice().sort((a, b) => a.at_ms - b.at_ms);
  let sourceCursor = trimStart, outputCursor = 0;
  for (const point of pauses) {
    const at = Math.max(sourceCursor, Number(point.at_ms));
    const segment = at - sourceCursor;
    if (editedMs <= outputCursor + segment) return sourceCursor + Math.max(0, editedMs - outputCursor);
    outputCursor += segment;
    const pause = Number(point.duration_ms || 0);
    if (editedMs <= outputCursor + pause) return at;
    outputCursor += pause; sourceCursor = at;
  }
  return Math.max(trimStart, Math.min(durationMs - trimEnd, sourceCursor + Math.max(0, editedMs - outputCursor)));
}

function updatePlayheads() {
  document.querySelectorAll('.timeline-clip[data-region-id]').forEach(clip => {
    const marker = clip.querySelector('.clip-playhead');
    if (!marker) return;
    const current = clip.dataset.regionId === state.playhead.regionId && clip.dataset.takeId === state.playhead.takeId;
    marker.hidden = !current;
    if (current) {
      const duration = Number(clip.dataset.durationMs || state.waveformDurations.get(`${clip.dataset.regionId}:${clip.dataset.takeId}`) || 1);
      marker.style.left = `${Math.max(0, Math.min(100, state.playhead.sourceMs / duration * 100))}%`;
    }
  });
}

async function scrubTake(region, take, sourceMs) {
  state.activeRegionId = region.id; state.selectedTakeId = take.id;
  state.playhead = {regionId: region.id, takeId: take.id, sourceMs};
  renderAll(false); updatePlayheads();
  $('#editPlayhead').value = Math.round(sourceMs);
  try {
    const preview = await api(`/api/director/edited-preview?region_id=${encodeURIComponent(region.id)}&take_id=${encodeURIComponent(take.id)}`);
    await playUrl(preview.audio_url, sourceToEditedMs(take.edits, sourceMs) / 1000, {
      mode: 'edited', regionId: region.id, takeId: take.id, edits: take.edits || {},
      durationMs: state.waveformDurations.get(`${region.id}:${take.id}`),
    });
  } catch (error) { toast(error.message, true); }
}

function renderTrackStack() {
  const host = $('#trackStack'); if (!host) return;
  host.innerHTML = '';
  const ruler = document.createElement('div'); ruler.className = 'track-ruler-row';
  ruler.innerHTML = `<div class="track-ruler-corner">${t('timeline.output')}</div>`;
  const rulerLane = document.createElement('div'); rulerLane.className = 'track-lane ruler-lane';
  state.data.project.regions.forEach(region => {
    const cell = document.createElement('span'); cell.style.width = `${regionWidth(region)}px`; cell.textContent = region.id; rulerLane.append(cell);
    const gap = document.createElement('i'); gap.style.width = `${timelineGapWidth(region)}px`; rulerLane.append(gap);
  });
  ruler.append(rulerLane); host.append(ruler);
  state.data.project.tracks.forEach(track => {
    const row = document.createElement('section');
    row.className = `track-row${monitorAudible(track.id) ? '' : ' inaudible'}`; row.dataset.trackId = track.id;
    const voice = state.data.voices.find(item => item.id === track.voice_mode);
    const strip = document.createElement('header'); strip.className = 'channel-strip';
    strip.innerHTML = `<i style="background:${track.color}"></i><div><strong>${escapeHtml(trackLabel(track))}</strong><small>${escapeHtml(voice?.label || track.voice_mode)}</small></div><div class="monitor-buttons"><button data-monitor="muted" class="${state.monitor.muted.has(track.id) ? 'active' : ''}">M</button><button data-monitor="solo" class="${state.monitor.solo.has(track.id) ? 'active' : ''}">S</button></div><label>${t('timeline.output.control')} <input data-track-gain type="number" min="-60" max="12" step="0.5" value="${track.output_gain_db ?? 0}"> dB</label>`;
    strip.querySelectorAll('[data-monitor]').forEach(button => button.onclick = () => toggleMonitor(track.id, button.dataset.monitor));
    strip.querySelector('[data-track-gain]').onchange = event => patchTrack(track.id, {output_gain_db: Number(event.target.value)});
    const lane = document.createElement('div'); lane.className = 'track-lane';
    state.data.project.regions.forEach(region => {
      const slot = document.createElement('div'); slot.className = 'clip-slot'; slot.style.width = `${regionWidth(region)}px`;
      if (region.track_id === track.id) {
        const take = timelineTake(region), audition = Boolean(take && take.id !== region.active_take_id), clip = document.createElement('button');
        clip.className = `timeline-clip${region.id === state.activeRegionId ? ' selected' : ''}${audition ? ' audition' : ''}${take ? '' : ' missing'}`;
        clip.dataset.regionId = region.id; clip.dataset.takeId = take?.id || '';
        clip.innerHTML = `<span><b>${region.id}</b><em>${take?.id || t('timeline.generate')}${audition ? ` · ${t('timeline.audition')}` : ''}</em></span><p>${escapeHtml(region.source_text.replace(tagPattern, '').trim())}</p>${take ? '<canvas></canvas><i class="clip-playhead" hidden></i>' : `<div class="missing-wave">${t('timeline.missing')}</div>`}<div class="clip-badges">${statusBadges(region)}</div>`;
        clip.onclick = () => selectRegion(region.id, true); slot.append(clip);
        if (take) {
          const canvas = clip.querySelector('canvas');
          canvas.onclick = event => {
            event.stopPropagation();
            const rect = canvas.getBoundingClientRect();
            const duration = state.waveformDurations.get(`${region.id}:${take.id}`) || Number(clip.dataset.durationMs) || Number(take.duration_seconds) * 1000;
            scrubTake(region, take, Math.max(0, Math.min(duration, (event.clientX - rect.left) / rect.width * duration)));
          };
          paintWaveform(canvas, region, take);
        }
      }
      lane.append(slot);
      const gap = document.createElement('button'); gap.className = 'lane-gap'; gap.style.width = `${timelineGapWidth(region)}px`; gap.title = t('timeline.pause.title', {ms: region.pause_after_ms}); gap.onclick = () => selectRegion(region.id, false); lane.append(gap);
    });
    row.append(strip, lane); host.append(row);
  });
  renderMonitoring();
  updatePlayheads();
}

async function patchTrack(trackId, patch) {
  try {
    state.data = await api('/api/director/save', {track: {id: trackId, ...patch}});
    renderAll(false); toast(t('track.saved'));
  } catch (error) { toast(error.message, true); }
}

function statusBadges(region) {
  const result = [];
  if (region.status.approved) result.push(`<span class="badge approved">${t('status.approved')}</span>`);
  if (region.status.generation_required) result.push(`<span class="badge stale">${t('status.generate')}</span>`);
  else result.push(`<span class="badge fresh">${t('status.fresh')}</span>`);
  if (region.status.qa_required) result.push('<span class="badge stale">QA</span>');
  return result.join('');
}

function renderTimeline() {
  renderTrackStack();
}

function selectionOffsets() {
  const selection = window.getSelection();
  if (!selection || !selection.rangeCount || !editor.contains(selection.anchorNode)) return state.selection;
  const range = selection.getRangeAt(0);
  const beforeStart = document.createRange();
  const beforeEnd = document.createRange();
  beforeStart.selectNodeContents(editor);
  beforeStart.setEnd(range.startContainer, range.startOffset);
  beforeEnd.selectNodeContents(editor);
  beforeEnd.setEnd(range.endContainer, range.endOffset);
  return {start: beforeStart.toString().length, end: beforeEnd.toString().length};
}

function pointAtOffset(offset) {
  const walker = document.createTreeWalker(editor, NodeFilter.SHOW_TEXT);
  let remaining = Math.max(0, offset), node, last = editor;
  while ((node = walker.nextNode())) {
    last = node;
    if (remaining <= node.nodeValue.length) return {node, offset: remaining};
    remaining -= node.nodeValue.length;
  }
  return last === editor ? {node: editor, offset: 0} : {node: last, offset: last.nodeValue.length};
}

function setSelectionOffsets(start, end = start) {
  const selection = window.getSelection();
  const range = document.createRange();
  const a = pointAtOffset(start), b = pointAtOffset(end);
  range.setStart(a.node, a.offset); range.setEnd(b.node, b.offset);
  selection.removeAllRanges(); selection.addRange(range);
  state.selection = {start, end};
}

function appendTaggedText(parent, text) {
  let cursor = 0;
  for (const match of text.matchAll(tagPattern)) {
    if (match.index > cursor) parent.append(document.createTextNode(text.slice(cursor, match.index)));
    const tag = document.createElement('span');
    tag.className = 'tag-token'; tag.textContent = match[0]; parent.append(tag);
    cursor = match.index + match[0].length;
  }
  if (cursor < text.length) parent.append(document.createTextNode(text.slice(cursor)));
}

function renderEditor(restore = true) {
  const first = activeRegion()?.start || 0;
  const saved = restore ? state.selection : {start: first, end: first};
  state.rendering = true;
  const fragment = document.createDocumentFragment();
  state.data.project.regions.forEach(region => {
    const span = document.createElement('span');
    span.className = `text-region${region.id === state.activeRegionId ? ' active' : ''}`;
    span.dataset.region = region.id;
    appendTaggedText(span, state.data.lab.text.slice(region.start, region.end));
    fragment.append(span);
  });
  editor.replaceChildren(fragment);
  editor.classList.toggle('tags-dimmed', state.tagsDimmed);
  state.rendering = false;
  const max = state.data.lab.text.length;
  state.selection = {start: Math.min(saved.start, max), end: Math.min(saved.end, max)};
  setSelectionOffsets(state.selection.start, state.selection.end);
  renderSelectionCue();
}

function regionForOffset(offset, preferredId = state.activeRegionId) {
  const preferred = state.data.project.regions.find(region => region.id === preferredId);
  if (preferred && offset >= preferred.start && offset <= preferred.end) return preferred;
  return state.data.project.regions.find(region => offset >= region.start && offset < region.end)
    || state.data.project.regions.at(-1);
}

function regionIdForNode(node) {
  const element = node?.nodeType === Node.ELEMENT_NODE ? node : node?.parentElement;
  return element?.closest?.('.text-region')?.dataset.region || null;
}

function selectionRegionHints() {
  const selection = window.getSelection();
  if (!selection || !selection.rangeCount || !editor.contains(selection.anchorNode)) return {};
  const range = selection.getRangeAt(0);
  return {
    start: regionIdForNode(range.startContainer),
    end: regionIdForNode(range.endContainer),
  };
}

function regionForSelection(offsets, hints = selectionRegionHints()) {
  const start = regionForOffset(offsets.start, hints.start || state.activeRegionId);
  const endProbe = offsets.end > offsets.start ? offsets.end - 1 : offsets.end;
  const end = regionForOffset(endProbe, hints.end || start?.id);
  return start && end && start.id === end.id ? start : null;
}

function renderSelectionCue(offsets = state.selection) {
  const hints = selectionRegionHints();
  const region = regionForSelection(offsets, hints);
  const startRegion = regionForOffset(offsets.start, hints.start || state.activeRegionId);
  const endProbe = offsets.end > offsets.start ? offsets.end - 1 : offsets.end;
  const endRegion = regionForOffset(endProbe, hints.end || startRegion?.id);
  const location = $('#activeRegionCue').parentElement;
  location.classList.toggle('crossing', !region);
  $('#activeRegionCue').textContent = region?.id || `${startRegion?.id || '?'}→${endRegion?.id || '?'}`;
  $('#selectionMeta').textContent = offsets.end > offsets.start
    ? region
      ? t('selection.single', {start: offsets.start, end: offsets.end})
      : t('selection.crossing')
    : t('selection.cursor', {offset: offsets.start});
}

function updateCursorRegion() {
  if (state.rendering) return;
  state.selection = selectionOffsets();
  const hints = selectionRegionHints();
  const region = regionForSelection(state.selection, hints)
    || regionForOffset(state.selection.start, hints.start || state.activeRegionId);
  renderSelectionCue();
  if (region.id !== state.activeRegionId) selectRegion(region.id, false);
}

function renderContext() {
  const regions = state.data.project.regions;
  const index = regionIndex();
  const region = activeRegion();
  const context = region.continuity_context;
  const previous = regions.slice(Math.max(0, index - context.previous_regions), index);
  const following = regions.slice(index + 1, index + 1 + context.next_regions);
  $('#contextPreview').innerHTML = [
    ...previous.map(item => `<div class="context-item"><b>← ${item.id}</b> ${escapeHtml(item.source_text.replace(tagPattern, '').trim().slice(-105))}<br><small>${item.active_take_id ? t('context.active', {take: item.active_take_id}) : t('context.noactive')}</small></div>`),
    ...following.map(item => `<div class="context-item"><b>${item.id} →</b> ${escapeHtml(item.source_text.replace(tagPattern, '').trim().slice(0, 105))}</div>`),
  ].join('') || `<div class="context-item">${t('context.empty')}</div>`;
}

function renderQuickTags() {
  $('#quickTags').innerHTML = '';
  const groups = [
    ['emotion', t('tags.group.emotion'), state.data.options.emotions],
    ['style', t('tags.group.style'), state.data.options.styles],
    ['prosody', t('tags.group.prosody'), state.data.options.expressiveness],
  ];
  groups.forEach(([category, label, values]) => {
    const group = document.createElement('div');
    group.className = 'quick-tag-group'; group.dataset.label = label;
    values.forEach(value => {
      const button = document.createElement('button');
      button.textContent = value; button.dataset.category = category;
      button.title = t('tags.insert.title', {category, value, region: activeRegion()?.id || 'region'});
      button.addEventListener('pointerdown', event => event.preventDefault());
      button.addEventListener('click', () => insertInlineTag(category, value));
      group.append(button);
    });
    $('#quickTags').append(group);
  });
}

function renderDiagnostics() {
  const take = selectedTake();
  $('#diagnostics').textContent = take ? JSON.stringify({
    take: take.id,
    approved: take.approved,
    fresh: take.fresh,
    audio_sha256: take.audio_sha256,
    transaction: take.transaction,
    qa_policy_snapshot: take.qa_policy_snapshot,
    qa_evaluations: take.qa_evaluations,
    qa_status: take.qa_status,
    approval_events: take.approval_events,
    human_judgement: take.human_judgement,
    edits: take.edits,
  }, null, 2) : t('diagnostics.empty');
}

function updateAutomation(kind, pointId, patch = null) {
  const key = kind === 'pause' ? 'inserted_pauses' : 'volume_envelope';
  commitSelectedEdits(take => ({
    [key]: (take.edits?.[key] || [])
      .filter(point => patch !== null || point.id !== pointId)
      .map(point => point.id === pointId && patch !== null ? {...point, ...patch} : point),
  }));
}

function renderAutomationPoints(take) {
  const host = $('#automationPoints'); host.innerHTML = '';
  if (!take) return;
  const edits = take.edits || {}, rows = [];
  (edits.volume_envelope || []).forEach(point => rows.push({kind:'gain', point}));
  (edits.inserted_pauses || []).forEach(point => rows.push({kind:'pause', point}));
  rows.sort((a, b) => a.point.at_ms - b.point.at_ms || a.kind.localeCompare(b.kind));
  if (!rows.length) {
    host.innerHTML = `<p class="microcopy">${t('automation.empty')}</p>`;
    return;
  }
  rows.forEach(({kind, point}) => {
    const row = document.createElement('div'); row.className = `automation-row ${kind}`;
    row.innerHTML = `<strong>${kind === 'pause' ? 'PAUSE' : 'GAIN'}</strong><label>${t('automation.at')}<input data-field="at_ms" type="number" min="0" step="10" value="${point.at_ms}"></label>${kind === 'pause' ? `<label>${t('automation.duration')}<input data-field="duration_ms" type="number" min="0" max="30000" step="10" value="${point.duration_ms}"></label>` : `<label>dB<input data-field="gain_db" type="number" min="-60" max="12" step="0.5" value="${point.gain_db}"></label>`}<button data-jump title="${t('automation.jump')}">⌖</button><button data-remove title="${t('automation.remove')}">×</button>`;
    row.querySelectorAll('input').forEach(input => input.onchange = () => updateAutomation(kind, point.id, {
      at_ms: Number(row.querySelector('[data-field=at_ms]').value),
      ...(kind === 'pause'
        ? {duration_ms: Number(row.querySelector('[data-field=duration_ms]').value)}
        : {gain_db: Number(row.querySelector('[data-field=gain_db]').value)}),
    }));
    row.querySelector('[data-jump]').onclick = () => {
      $('#editPlayhead').value = point.at_ms;
      state.playhead = {regionId: activeRegion().id, takeId: take.id, sourceMs: point.at_ms}; updatePlayheads();
    };
    row.querySelector('[data-remove]').onclick = () => updateAutomation(kind, point.id, null);
    host.append(row);
  });
}

function renderExportHistory() {
  const host = $('#exportHistory'), project = state.data.project;
  const revisions = (project.export_revisions || []).slice().reverse().slice(0, 6);
  host.innerHTML = revisions.length ? `<strong>${t('export.history')}</strong>` : '';
  revisions.forEach(revision => {
    const base = `/exports/${encodeURIComponent(project.project_id)}/${encodeURIComponent(revision.id)}`;
    const item = document.createElement('div'); item.className = 'export-revision';
    item.innerHTML = `<span><b>${revision.id}${project.latest_export_revision_id === revision.id ? ` · ${t('export.latest')}` : ''}</b><small>${escapeHtml(revision.label || t('export.unlabelled'))}${revision.notes ? ` · ${escapeHtml(revision.notes)}` : ''}</small></span><span><a href="${base}/master.wav" download>WAV</a><a href="${base}/delivery.mp3" download>MP3</a><a href="${base}/manifest.json" download>manifest</a></span>`;
    host.append(item);
  });
}

function renderInspector() {
  const region = activeRegion();
  const track = effectiveTrack(region);
  const clean = region.source_text.replace(tagPattern, '').trim();
  $('#regionTitle').textContent = `${region.id} · ${clean.slice(0, 72)}${clean.length > 72 ? '…' : ''}`;
  $('#inspectorRegion').textContent = region.id;
  $('#regionState').innerHTML = statusBadges(region);
  const trackSelect = $('#regionTrack'); trackSelect.innerHTML = '';
  state.data.project.tracks.forEach(item => addOption(trackSelect, item.id, trackLabel(item)));
  trackSelect.value = region.track_id;
  const voiceSelect = $('#regionVoice'); voiceSelect.innerHTML = '';
  addOption(voiceSelect, '', t('voice.inherited', {voice: state.data.voices.find(item => item.id === track.voice_mode)?.label || track.voice_mode}));
  state.data.voices.forEach(voice => addOption(voiceSelect, voice.id, voice.label));
  voiceSelect.value = region.overrides.voice_mode || '';
  fillSelect($('#emotion'), state.data.options.emotions, effectiveControl(region, 'emotion'));
  fillSelect($('#style'), state.data.options.styles, effectiveControl(region, 'style'));
  fillSelect($('#speed'), state.data.options.speeds, effectiveControl(region, 'speed'));
  fillSelect($('#pitch'), state.data.options.pitches, effectiveControl(region, 'pitch'));
  fillSelect($('#expressiveness'), state.data.options.expressiveness, effectiveControl(region, 'expressiveness'));
  $('#temperature').value = region.overrides.generation.temperature ?? state.data.project.generation_defaults.temperature;
  $('#topK').value = region.overrides.generation.top_k ?? state.data.project.generation_defaults.top_k;
  $('#seed').value = region.overrides.generation.seed ?? state.data.project.generation_defaults.seed;
  $('#maxTokens').value = region.overrides.generation.max_tokens ?? state.data.project.generation_defaults.max_tokens;
  $('#contextPrevious').value = region.continuity_context.previous_regions;
  $('#contextNext').value = region.continuity_context.next_regions;
  $('#pauseAfter').value = region.pause_after_ms;
  $('#pauseValue').textContent = `${(region.pause_after_ms / 1000).toFixed(2)} s`;
  const take = selectedTake(region), edits = take?.edits || {};
  $('#trimStart').value = edits.trim_start_ms || 0; $('#trimEnd').value = edits.trim_end_ms || 0;
  $('#fadeIn').value = edits.fade_in_ms || 0; $('#fadeOut').value = edits.fade_out_ms || 0;
  $('#clipGain').value = edits.clip_gain_db || 0;
  $('#editSummary').textContent = take
    ? t('edit.summary', {take: take.id, pauses: (edits.inserted_pauses || []).length, points: (edits.volume_envelope || []).length})
    : t('edit.choose');
  ['trimStart','trimEnd','fadeIn','fadeOut','clipGain','editPlayhead','listenRaw','listenEdited','addPause','addEnvelope','clearAutomation'].forEach(id => { $(`#${id}`).disabled = !take; });
  renderAutomationPoints(take); renderExportHistory();
  const policy = state.data.project.qa_policy || {};
  $('#speakerGate').checked = policy.speaker_similarity?.enabled !== false;
  $('#asrGate').checked = policy.asr_content?.enabled !== false;
  const qa = take?.qa_status;
  const running = (state.data.qa_jobs || []).find(job => job.region_id === region.id && job.take_id === take?.id && ['queued','running'].includes(job.state));
  $('#qaMeta').textContent = take ? t('qa.meta', {take: take.id, status: qa?.overall || 'missing', running: running ? ` · ${running.state}` : ''}) : t('qa.empty');
  $('#runQA').disabled = !take || Boolean(running);
  renderContext(); renderDiagnostics(); renderQuickTags();
}

function renderTakes() {
  const region = activeRegion();
  const container = $('#takes');
  $('#takesTitle').textContent = t('takes.count', {count: region.takes.length, region: region.id});
  container.innerHTML = '';
  if (!region.takes.length) {
    container.innerHTML = `<div class="empty">${t('takes.empty')}</div>`;
    state.selectedTakeId = null; renderAB(); renderDiagnostics(); return;
  }
  if (!region.takes.some(take => take.id === state.selectedTakeId)) {
    state.selectedTakeId = region.active_take_id || region.takes.at(-1).id;
  }
  region.takes.slice().reverse().forEach(take => {
    const node = document.createElement('article');
    node.className = `take${take.id === region.active_take_id ? ' active' : ''}${take.id === state.selectedTakeId ? ' selected' : ''}`;
    const operation = take.transaction?.operation || 'unknown';
    const qaState = take.qa_status?.overall || 'missing';
    node.innerHTML = `<div class="take-title"><strong>${take.id}</strong><small>${operation} · seed ${take.transaction?.higgs_request?.seed ?? '?'}</small><div class="take-flags">${take.approved ? `<span class="badge approved">${t('status.approved')}</span>` : ''}${take.fresh ? `<span class="badge fresh">${t('status.fresh')}</span>` : `<span class="badge stale">${t('status.stale')}</span>`}<span class="badge ${qaState === 'pass' ? 'approved' : 'stale'}">QA ${qaState}</span></div></div><div class="take-audio"><audio controls preload="metadata" src="${takeUrl(take)}"></audio></div><div class="take-actions"><button data-act="select">${t('take.details')}</button><button data-act="activate" class="${take.id === region.active_take_id ? 'active' : ''}">${t(take.id === region.active_take_id ? 'take.active' : 'take.activate')}</button><button data-act="approve" class="${take.approved ? 'good' : ''}">${take.approved ? t('take.approved') : t('take.approve')}</button><button data-act="a">${t('take.seta')}</button><button data-act="b">${t('take.setb')}</button><button data-act="context">${t('take.context')}</button></div><div class="take-note"><input value="${escapeHtml(take.human_judgement?.notes || '')}" placeholder="${t('take.note.placeholder')}"><button data-act="note">${t('take.note.save')}</button><button data-act="copy">${t('take.edits.copy')}</button></div>`;
    const audio = node.querySelector('audio');
    audio.onplay = () => { if (!canAudition(region)) audio.pause(); };
    node.querySelector('[data-act=select]').onclick = () => {
      state.selectedTakeId = take.id;
      renderTrackStack();
      renderTakes();
      renderInspector();
    };
    node.querySelector('[data-act=activate]').onclick = () => takeAction(take, 'activate');
    node.querySelector('[data-act=approve]').onclick = () => approveTake(take);
    node.querySelector('[data-act=note]').onclick = () => takeAction(take, 'note', {notes: node.querySelector('input').value});
    node.querySelector('[data-act=copy]').onclick = () => {
      const source = edlTake(region); if (!source || source.id === take.id) return toast(t('take.edits.no_source'), true);
      if (confirm(t('take.edits.confirm', {source: source.id, target: take.id}))) takeAction(take, 'copy_edits', {from_take_id: source.id});
    };
    node.querySelector('[data-act=a]').onclick = () => setABSlot('a', take);
    node.querySelector('[data-act=b]').onclick = () => setABSlot('b', take);
    node.querySelector('[data-act=context]').onclick = () => { state.selectedTakeId = take.id; auditionContext(); };
    container.append(node);
  });
  renderAB(); renderInspector();
}

function directorTake(region, id) { return region.takes.find(take => take.id === id); }

function resetABForRegion(region) {
  if (state.ab.regionId === region.id) return;
  const candidates = region.takes.slice().reverse();
  const first = region.active_take_id || candidates[0]?.id || null;
  state.ab = {
    regionId: region.id,
    a: first,
    b: candidates.find(take => take.id !== first)?.id || null,
    playing: 'a',
  };
  const audio = $('#abAudio'); audio.pause(); audio.removeAttribute('src'); audio.load();
}

function setABSlot(slot, take) {
  resetABForRegion(activeRegion());
  state.ab[slot] = take.id; state.ab.playing = slot;
  state.selectedTakeId = take.id; renderAB(); renderInspector();
}

function renderAB() {
  const region = activeRegion(); resetABForRegion(region);
  const a = directorTake(region, state.ab.a), b = directorTake(region, state.ab.b);
  $('#abA').textContent = `A · ${a?.id || '—'}`;
  $('#abB').textContent = `B · ${b?.id || '—'}`;
  $('#abA').classList.toggle('active', state.ab.playing === 'a');
  $('#abB').classList.toggle('active', state.ab.playing === 'b');
  $('#abSummary').textContent = a && b ? `${a.id} ↔ ${b.id}` : t('ab.need.two');
  $('#abToggle').disabled = !(a && b);
  $('#abContext').disabled = !activeTake(region);
}

function switchAB(slot) {
  if (!canAudition(activeRegion())) return;
  const take = directorTake(activeRegion(), state.ab[slot]);
  if (!take) { toast(t('ab.empty', {slot: slot.toUpperCase()}), true); return; }
  const audio = $('#abAudio'), wasPlaying = !audio.paused, position = audio.currentTime || 0;
  state.ab.playing = slot; state.selectedTakeId = take.id;
  audio.src = takeUrl(take); audio.load();
  audio.addEventListener('loadedmetadata', () => {
    audio.currentTime = Math.min(position, Math.max(0, audio.duration - .01));
    if (wasPlaying) audio.play().catch(() => {});
  }, {once: true});
  renderAB(); renderInspector();
}

function selectRegion(id, scroll) {
  state.activeRegionId = id; state.selectedTakeId = null;
  const region = activeRegion();
  if (scroll && region) {
    state.selection = {start: region.start, end: region.start};
    setSelectionOffsets(region.start, region.start);
  }
  renderTrackStack(); renderInspector(); renderTakes();
  editor.querySelectorAll('.text-region').forEach(node => node.classList.toggle('active', node.dataset.region === id));
  renderSelectionCue();
  if (scroll) editor.querySelector(`[data-region="${id}"]`)?.scrollIntoView({block: 'center', behavior: 'smooth'});
}

function renderAll(editorToo = false) {
  renderEngine(); renderSummary(); renderWorkflow(); renderTrackStack();
  if (editorToo) renderEditor(true);
  else renderSelectionCue();
  renderInspector(); renderTakes();
}

async function refresh(preserve = true) {
  const oldId = state.activeRegionId;
  state.data = await api('/api/director/state');
  state.activeRegionId = preserve && state.data.project.regions.some(region => region.id === oldId)
    ? oldId : state.data.project.regions[0]?.id;
  $('#projectTitle').textContent = `${state.data.project.title} · TTS Directors Room`;
  renderEngine(); renderSummary(); renderWorkflow(); renderTrackStack(); renderEditor(preserve); renderInspector(); renderTakes();
}

async function patchRegion(patch) {
  const region = activeRegion();
  try {
    state.data = await api('/api/director/save', {region: {id: region.id, ...patch}});
    renderAll(false); toast(t('region.saved'));
  } catch (error) { toast(error.message, true); }
}

async function patchGeneration() {
  const values = {
    temperature: Number($('#temperature').value), top_k: Number($('#topK').value),
    seed: Number($('#seed').value), max_tokens: Number($('#maxTokens').value),
  };
  try {
    state.data = await api('/api/director/save', {generation_defaults: values});
    renderAll(false); toast(t('sampling.saved'));
  } catch (error) { toast(error.message, true); }
}

async function patchDelivery() {
  const controls = {
    emotion: $('#emotion').value, style: $('#style').value, speed: $('#speed').value,
    pitch: $('#pitch').value, expressiveness: $('#expressiveness').value,
  };
  try {
    state.data = await api('/api/director/save', {region: {id: activeRegion().id, overrides: {controls}}});
    renderAll(false); toast(t('delivery.saved'));
  } catch (error) { toast(error.message, true); }
}

async function saveManuscript(action = 'manuscript_edit') {
  if (state.rendering) return;
  clearTimeout(state.saveTimer); state.saveTimer = null;
  $('#saveState').textContent = t('save.saving');
  try {
    state.selection = selectionOffsets();
    state.data = await api('/api/director/save', {
      manuscript: editor.textContent,
      manuscript_action: action,
      active_region_id: state.activeRegionId,
    });
    state.activeRegionId = regionForOffset(state.selection.start)?.id || state.data.project.regions[0].id;
    renderAll(true); $('#saveState').textContent = t('save.saved');
  } catch (error) {
    toast(error.message, true); $('#saveState').textContent = t('save.rejected');
    try { await refresh(true); } catch (_) {}
  }
}

async function historyAction(action) {
  if (state.busy) return;
  try {
    if (state.saveTimer) await saveManuscript();
    state.busy = true;
    const previousRegion = state.activeRegionId;
    state.data = await api('/api/director/history', {action});
    state.activeRegionId = state.data.project.regions.some(region => region.id === previousRegion)
      ? previousRegion : state.data.project.regions[0]?.id;
    state.selectedTakeId = null;
    renderAll(true);
    toast(state.data.history_changed ? t('history.applied', {action: action === 'undo' ? 'Undo' : 'Redo'}) : t('history.none', {action}));
  } catch (error) { toast(error.message, true); }
  finally { state.busy = false; }
}

function scheduleSave() {
  if (state.rendering) return;
  $('#saveState').textContent = t('save.unsaved');
  clearTimeout(state.saveTimer); state.saveTimer = setTimeout(saveManuscript, 500);
}

function insertInlineTag(category, value) {
  const offsets = selectionOffsets(), text = editor.textContent, token = `<|${category}:${value}|>`;
  const region = regionForSelection(offsets);
  if (!region) { toast(t('inline.crossing'), true); return; }
  if (text.slice(Math.max(region.start, offsets.start - token.length), offsets.start) === token) {
    toast(t('inline.duplicate')); return;
  }
  state.selection = {start: offsets.start + token.length, end: offsets.end + token.length};
  editor.textContent = text.slice(0, offsets.start) + token + text.slice(offsets.start);
  setSelectionOffsets(state.selection.start, state.selection.end);
  saveManuscript('inline_direction_add');
}

function removeInlineTag() {
  const offsets = selectionOffsets(), text = editor.textContent;
  const region = regionForSelection(offsets);
  if (!region) { toast(t('inline.remove.crossing'), true); return; }
  const matches = [...text.matchAll(tagPattern)].map(match => ({
    start: match.index, end: match.index + match[0].length, token: match[0],
  })).filter(match => match.start >= region.start && match.end <= region.end);
  let selected = offsets.end > offsets.start
    ? matches.filter(match => match.start < offsets.end && match.end > offsets.start)
    : matches.filter(match => match.start <= offsets.start && match.end >= offsets.start);
  if (!selected.length) {
    selected = matches.filter(match => match.end <= offsets.start && !text.slice(match.end, offsets.start).trim()).slice(-1);
  }
  if (!selected.length) { toast(t('inline.remove.none'), true); return; }
  let next = text;
  for (const match of selected.slice().sort((a, b) => b.start - a.start)) {
    next = next.slice(0, match.start) + next.slice(match.end);
  }
  const removedBeforeStart = selected.filter(match => match.end <= offsets.start).reduce((sum, match) => sum + match.token.length, 0);
  const removedBeforeEnd = selected.filter(match => match.end <= offsets.end).reduce((sum, match) => sum + match.token.length, 0);
  state.selection = {
    start: Math.max(region.start, offsets.start - removedBeforeStart),
    end: Math.max(region.start, offsets.end - removedBeforeEnd),
  };
  editor.textContent = next;
  setSelectionOffsets(state.selection.start, state.selection.end);
  saveManuscript('inline_direction_remove');
}

async function generate(operation) {
  if (state.busy) return;
  const region = activeRegion(), base = activeTake(region);
  if (operation === 'retry_exact' && !base) { toast(t('generate.no.base'), true); return; }
  state.busy = true;
  document.querySelectorAll('.generation-actions button').forEach(button => { button.disabled = true; });
  toast(t('generate.started', {operation, region: region.id}));
  const poller = setInterval(async () => {
    try {
      const snapshot = await api('/api/director/state');
      state.data.generation_activity = snapshot.generation_activity;
      renderGenerationActivity();
    } catch (_) {}
  }, 700);
  try {
    const result = await api('/api/director/generate', {region_id: region.id, operation, base_take_id: base?.id});
    state.data = result.state; state.selectedTakeId = result.take.id; state.data.engine.model_loaded = true;
    renderAll(false);
    const generated = state.data.project.regions.find(item => item.id === region.id).takes.find(item => item.id === result.take.id);
    new Audio(takeUrl(generated)).play().catch(() => {});
    toast(t('generate.created', {take: result.take.id}));
  } catch (error) { toast(error.message, true); }
  finally {
    clearInterval(poller); state.busy = false;
    document.querySelectorAll('.generation-actions button').forEach(button => { button.disabled = false; });
  }
}

async function takeAction(take, action, extra = {}) {
  try {
    state.data = await api('/api/director/take', {region_id: activeRegion().id, take_id: take.id, action, ...extra});
    state.selectedTakeId = take.id; renderAll(false);
    toast(t(action === 'activate' ? 'take.action.activate' : action === 'approve' ? 'take.action.approve' : 'take.action.metadata'));
  } catch (error) { toast(error.message, true); }
}

async function approveTake(take) {
  if (take.approved) return takeAction(take, 'approve', {approved: false});
  let override_reason = '';
  if (take.qa_status?.required) {
    override_reason = prompt(t('take.approve.override', {status: take.qa_status.overall})) || '';
    if (!override_reason.trim()) return;
  }
  return takeAction(take, 'approve', {approved: true, override_reason});
}

function commitSelectedEdits(extra = {}) {
  const region = activeRegion(), take = selectedTake(region); if (!take) return Promise.resolve();
  const target = {regionId: region.id, takeId: take.id};
  const controls = {
    trim_start_ms: Number($('#trimStart').value), trim_end_ms: Number($('#trimEnd').value),
    fade_in_ms: Number($('#fadeIn').value), fade_out_ms: Number($('#fadeOut').value),
    clip_gain_db: Number($('#clipGain').value),
  };
  state.editQueue = state.editQueue.then(async () => {
    const latestRegion = state.data.project.regions.find(item => item.id === target.regionId);
    const latestTake = latestRegion && directorTake(latestRegion, target.takeId);
    if (!latestTake) throw new Error(t('edit.target.missing'));
    const dynamic = typeof extra === 'function' ? extra(latestTake) : extra;
    const edits = {
      ...controls,
      inserted_pauses: latestTake.edits?.inserted_pauses || [],
      volume_envelope: latestTake.edits?.volume_envelope || [],
      ...dynamic,
    };
    state.data = await api('/api/director/take', {
      region_id: target.regionId, take_id: target.takeId, action: 'edit', edits,
    });
    renderAll(false); toast(t('edit.saved', {take: target.takeId}));
  }).catch(error => { toast(error.message, true); });
  return state.editQueue;
}

async function playUrl(url, start = 0, context = null) {
  transportAudio.pause(); state.transport = context;
  transportAudio.src = url; transportAudio.currentTime = Math.max(0, start);
  await transportAudio.play();
}

async function listenEditedTake(region = activeRegion(), take = selectedTake(region)) {
  if (!take) return toast(t('audition.no.take'), true);
  if (!canAudition(region)) return;
  try {
    const preview = await api(`/api/director/edited-preview?region_id=${encodeURIComponent(region.id)}&take_id=${encodeURIComponent(take.id)}`);
    await playUrl(preview.audio_url, 0, {
      mode: 'edited', regionId: region.id, takeId: take.id, edits: take.edits || {},
      durationMs: state.waveformDurations.get(`${region.id}:${take.id}`) || Number(take.duration_seconds) * 1000,
    });
    toast(t('audition.edited'));
  } catch (error) { toast(error.message, true); }
}

async function patchQaPolicy() {
  try {
    state.data = await api('/api/director/save', {qa_policy: {
      speaker_similarity: {enabled: $('#speakerGate').checked},
      asr_content: {enabled: $('#asrGate').checked},
    }});
    renderAll(false); toast(t('qa.saved'));
  } catch (error) { toast(error.message, true); }
}

async function runSelectedQA() {
  const region = activeRegion(), take = selectedTake(region); if (!take) return;
  try {
    await api('/api/director/qa', {region_id: region.id, take_id: take.id, operation: 'recheck'});
    toast(t('qa.queued'));
    const poll = setInterval(async () => {
      try {
        await refresh(true);
        const running = (state.data.qa_jobs || []).some(job => job.region_id === region.id && job.take_id === take.id && ['queued','running'].includes(job.state));
        if (!running) clearInterval(poll);
      } catch (_) {}
    }, 1200);
  } catch (error) { toast(error.message, true); }
}

async function playSequence(items) {
  transportAudio.pause(); state.transport = null;
  for (const item of items.filter(item => item?.url)) {
    await new Promise(resolve => {
      const audio = transportAudio; let timer;
      audio.src = item.url;
      audio.addEventListener('loadedmetadata', () => {
        audio.currentTime = Math.max(0, item.tail ? audio.duration - item.duration : item.start || 0);
        audio.play().catch(resolve);
        timer = setTimeout(() => { audio.pause(); resolve(); }, item.duration * 1000);
      }, {once: true});
      audio.addEventListener('error', resolve, {once: true}); audio.addEventListener('ended', resolve, {once: true});
    });
  }
}

async function auditionContext() {
  const regions = state.data.project.regions, index = regionIndex(), current = selectedTake();
  const voice = state.data.voices.find(item => item.id === effectiveVoice(activeRegion()));
  const previous = regions[index - 1], following = regions[index + 1];
  const previousTake = previous?.takes.find(take => take.id === previous.active_take_id);
  const nextTake = following?.takes.find(take => take.id === following.active_take_id);
  if (!current) { toast(t('context.no.current'), true); return; }
  if (!canAudition(activeRegion())) return;
  toast(t('context.audition'));
  const processed = async (region, take) => take
    ? (await api(`/api/director/edited-preview?region_id=${encodeURIComponent(region.id)}&take_id=${encodeURIComponent(take.id)}`)).audio_url
    : null;
  await playSequence([
    {url: voice.preview_url, duration: .8},
    {url: previousTake && canAudition(previous, false) && await processed(previous, previousTake), duration: .8, tail: true},
    {url: await processed(activeRegion(), current), duration: 1.4},
    {url: nextTake && canAudition(following, false) && await processed(following, nextTake), duration: .8},
  ]);
}

async function renderStory() {
  try {
    $('#renderMeta').textContent = t('render.progress');
    const result = await api('/api/director/render', {});
    state.latestRender = result; $('#renderAudio').src = result.audio_url; $('#renderAudio').hidden = false;
    $('#renderMeta').textContent = t('render.result', {cache: t(result.cached ? 'render.cache.hit' : 'render.cache.new'), seconds: result.duration_seconds, hash: result.edl_sha256.slice(0, 10)});
    await refresh(true);
    $('#renderAudio').play().catch(() => {}); toast(t('render.done'));
  } catch (error) { toast(error.message, true); }
}

async function startBaselineFirstRead() {
  try {
    const voice_mode = $('#baselineVoice').value;
    if (!voice_mode) throw new Error(t('baseline.voice.required'));
    const activity = await api('/api/director/baseline/start', {voice_mode});
    state.data.baseline.activity = activity;
    state.baselineNotice = null;
    renderAll(false);
    toast(t('baseline.started'));
  } catch (error) { toast(error.message, true); }
}

async function cancelBaselineFirstRead() {
  try {
    const activity = await api('/api/director/baseline/cancel', {});
    state.data.baseline.activity = activity;
    renderAll(false);
    toast(t('baseline.cancelled'));
  } catch (error) { toast(error.message, true); }
}

async function exportRender() {
  try {
    const renderId = state.latestRender?.id || state.data.workflow?.last_render_id;
    const preflight = await api('/api/director/export/preflight', {render_id: renderId});
    if (preflight.errors.length) {
      const visible = preflight.errors.slice(0, 6).map(item => item.message).join(' ');
      throw new Error(`${visible}${preflight.errors.length > 6 ? t('export.more', {count: preflight.errors.length - 6}) : ''}`);
    }
    let override_reason = '';
    if (preflight.warnings.length) {
      override_reason = prompt(t('export.warning', {count: preflight.warnings.length})) || '';
      if (!override_reason.trim()) return;
    }
    const label = prompt(t('export.label'), '') || '';
    const notes = prompt(t('export.notes'), '') || '';
    const result = await api('/api/director/export', {render_id: renderId, preflight_fingerprint: preflight.preflight_fingerprint, override_reason, label, notes});
    $('#renderMeta').innerHTML = `${t('export.prefix')} ${Object.entries(result.files).map(([name, url]) => `<a href="${url}" download>${name}</a>`).join(' · ')}`;
    await refresh(true);
    toast(t('export.done', {result: t(result.reused ? 'export.reused' : 'export.created'), id: result.id}));
  } catch (error) { toast(error.message, true); }
}

function bind() {
  document.querySelectorAll('[data-locale]').forEach(button => {
    button.onclick = () => setUiLocale(button.dataset.locale);
  });
  editor.addEventListener('input', scheduleSave);
  editor.addEventListener('keyup', updateCursorRegion);
  editor.addEventListener('pointerup', updateCursorRegion);
  document.addEventListener('selectionchange', () => { if (editor.contains(window.getSelection()?.anchorNode)) updateCursorRegion(); });
  $('#previousRegion').onclick = () => selectRegion(state.data.project.regions[Math.max(0, regionIndex() - 1)].id, true);
  $('#nextRegion').onclick = () => selectRegion(state.data.project.regions[Math.min(state.data.project.regions.length - 1, regionIndex() + 1)].id, true);
  $('#playActive').onclick = () => listenEditedTake();
  $('#listenReference').onclick = () => { if (!canAudition(activeRegion())) return; const voice = state.data.voices.find(item => item.id === effectiveVoice(activeRegion())); playUrl(voice.preview_url); };
  $('#auditionContext').onclick = auditionContext;
  $('#abA').onclick = () => switchAB('a'); $('#abB').onclick = () => switchAB('b');
  $('#abToggle').onclick = () => switchAB(state.ab.playing === 'a' ? 'b' : 'a');
  $('#abContext').onclick = auditionContext;
  $('#regionTrack').onchange = event => patchRegion({track_id: event.target.value});
  $('#regionVoice').onchange = event => patchRegion({overrides: {voice_mode: event.target.value || null}});
  const patchContext = () => patchRegion({continuity_context: {previous_regions: Number($('#contextPrevious').value), next_regions: Number($('#contextNext').value)}});
  $('#contextPrevious').onchange = patchContext; $('#contextNext').onchange = patchContext;
  $('#pauseAfter').oninput = () => { $('#pauseValue').textContent = `${(Number($('#pauseAfter').value) / 1000).toFixed(2)} s`; };
  $('#pauseAfter').onchange = () => patchRegion({pause_after_ms: Number($('#pauseAfter').value)});
  ['trimStart','trimEnd','fadeIn','fadeOut','clipGain'].forEach(id => { $(`#${id}`).onchange = () => commitSelectedEdits(); });
  $('#listenRaw').onclick = () => { const take = selectedTake(); if (take && canAudition(activeRegion())) playUrl(takeUrl(take), 0, {mode:'raw', regionId:activeRegion().id, takeId:take.id}); };
  $('#listenEdited').onclick = () => listenEditedTake();
  $('#addPause').onclick = () => {
    const at = Number($('#editPlayhead').value || 0), id = `pause-${Date.now()}`;
    commitSelectedEdits(take => ({inserted_pauses: [...(take.edits?.inserted_pauses || []), {id, at_ms:at, duration_ms:500}]}));
  };
  $('#addEnvelope').onclick = () => {
    const at = Number($('#editPlayhead').value || 0), id = `env-${Date.now()}`;
    const gain = Number(prompt(t('gain.prompt'), '0'));
    if (Number.isFinite(gain)) commitSelectedEdits(take => ({volume_envelope: [...(take.edits?.volume_envelope || []), {id, at_ms:at, gain_db:gain}]}));
  };
  $('#clearAutomation').onclick = () => commitSelectedEdits({fade_in_ms:0, fade_out_ms:0, volume_envelope:[], inserted_pauses:[]});
  $('#speakerGate').onchange = patchQaPolicy; $('#asrGate').onchange = patchQaPolicy; $('#runQA').onclick = runSelectedQA;
  $('#timelineZoom').oninput = event => { state.timelineZoom = Number(event.target.value); renderTrackStack(); };
  $('#clearMonitoring').onclick = () => { state.monitor.muted.clear(); state.monitor.solo.clear(); renderTrackStack(); };
  transportAudio.addEventListener('timeupdate', () => {
    const context = state.transport; if (!context) return;
    const outputMs = transportAudio.currentTime * 1000;
    const sourceMs = context.mode === 'edited'
      ? editedToSourceMs(context.edits, outputMs, context.durationMs)
      : outputMs;
    state.playhead = {regionId: context.regionId, takeId: context.takeId, sourceMs};
    if (!$('#editPlayhead').matches(':focus') && activeRegion().id === context.regionId && selectedTake()?.id === context.takeId) $('#editPlayhead').value = Math.round(sourceMs);
    updatePlayheads();
  });
  $('#abAudio').addEventListener('play', event => { if (!canAudition(activeRegion())) event.currentTarget.pause(); });
  ['emotion','style','speed','pitch','expressiveness'].forEach(id => { $(`#${id}`).onchange = patchDelivery; });
  ['temperature','topK','seed','maxTokens'].forEach(id => { $(`#${id}`).onchange = patchGeneration; });
  $('#retryExact').onclick = () => generate('retry_exact'); $('#newPerformance').onclick = () => generate('new_performance'); $('#variation').onclick = () => generate('variation');
  $('#renderStory').onclick = renderStory; $('#exportRender').onclick = exportRender;
  $('#baselineEntry').onclick = () => { renderBaseline(); $('#baselineDialog').showModal(); };
  $('#closeBaseline').onclick = () => $('#baselineDialog').close();
  $('#dismissBaseline').onclick = () => $('#baselineDialog').close();
  $('#listenBaselineReference').onclick = () => {
    const voice = state.data.voices.find(item => item.id === $('#baselineVoice').value);
    if (voice) playUrl(voice.preview_url);
  };
  $('#startBaseline').onclick = startBaselineFirstRead;
  $('#cancelBaseline').onclick = cancelBaselineFirstRead;
  $('#undoProject').onclick = () => historyAction('undo');
  $('#redoProject').onclick = () => historyAction('redo');
  $('#removeInlineTag').addEventListener('pointerdown', event => event.preventDefault());
  $('#removeInlineTag').onclick = removeInlineTag;
  $('#toggleRaw').onclick = () => {
    state.tagsDimmed = !state.tagsDimmed;
    editor.classList.toggle('tags-dimmed', state.tagsDimmed);
    $('#toggleRaw').textContent = t(state.tagsDimmed ? 'tags.dimmed' : 'tags.clear');
  };
  $('#playStory').onclick = () => state.latestRender ? playUrl(state.latestRender.audio_url) : toast(t('story.render.first'), true);
  $('#playSelection').onclick = () => toast(t('selection.future'));
  document.addEventListener('keydown', event => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'z') {
      event.preventDefault(); historyAction(event.shiftKey ? 'redo' : 'undo'); return;
    }
    if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') { event.preventDefault(); generate('new_performance'); }
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 's') { event.preventDefault(); saveManuscript(); }
  });
}

async function start() {
  try {
    i18n.applyStatic(); updateLocaleSwitch();
    await refresh(false); bind();
    toast(t('ready.toast'));
  } catch (error) { toast(error.message, true); }
}

start();
