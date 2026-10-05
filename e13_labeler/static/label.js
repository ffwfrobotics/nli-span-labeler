// E13 labelling screen: requirements §6.1 (layout), §6.2 (keyboard map),
// FR-12 to FR-22. Plain JS, no build step.

const ROLES = { s: 'support', r: 'refute', u: 'unsupported', m: 'framing' };
const SKIP_CODES = ['cannot_judge', 'broken_item', 'offensive', 'too_long', 'other'];
const FLAG_KINDS = ['bad_item', 'guideline_unclear', 'other'];
const CANDIDATE_REASONS = new Set(['stale_state', 'subjective']);
const REASON_KEYS = ['1', '2', '3', '4', '5', '6', '7', '8', '9', '0'];
const IDLE_MS = 60000;  // FR-22: active only with an interaction in the last 60 s
// noul defaults when criteria are absent; wording from the E09 student code
// (docs/e13/fixtures/README.md, "How each question type maps to options")
const NOUL_DEFAULT = { true: 'The statement holds.', false: 'The statement does not hold.' };

let L = null;           // the item being labelled, and everything the labeler did to it
let reasonDefs = {};    // reason -> definition, from /api/reasons (shown as tooltips)
let containers = {};    // container id -> {side, pointer, option, text, bare}

// ============================================================================
// Loading and rendering
// ============================================================================

async function loadNextItem() {
    let resp;
    try {
        resp = await authenticatedFetch('/api/next');
    } catch (e) {
        return;
    }
    if (resp.status === 404 || resp.status === 403) {
        const detail = (await resp.json()).detail;
        if (detail && detail.agreement) return showAgreement();
        if (detail && detail.quiz && typeof showOnboarding === 'function') return showOnboarding(detail);
        showEmpty(resp.status === 404 ? 'No items to label right now. Check back when a batch is open.'
            : (typeof detail === 'string' ? detail : detail.message));
        return;
    }
    if (resp.status === 409) {
        return loadNextItem();  // lost a lock race; ask again
    }
    renderItem(await resp.json());
}

function showEmpty(text) {
    L = null;
    document.getElementById('label-main').classList.add('hidden');
    document.getElementById('label-footer').classList.add('hidden');
    document.getElementById('label-empty').classList.remove('hidden');
    document.getElementById('label-empty-text').textContent = text;
}

function renderItem(item) {
    containers = {};
    const qid = item.item_id.split('#').pop();
    L = {
        item,
        qid,
        options: buildOptions(item.question),
        reasons: new Set(),
        answerable: false,
        active: null,
        spans: [],
        focusedSpan: -1,
        selection: null,
        region: 'state',
        cursor: -1,
        anchor: -1,
        mode: null,
        pendingRole: null,
        history: [],
        timer: { active: 0, last: Date.now(), lastInteraction: Date.now() },
    };

    document.getElementById('label-empty').classList.add('hidden');
    document.getElementById('label-main').classList.remove('hidden');
    document.getElementById('label-footer').classList.remove('hidden');
    document.getElementById('note').value = '';
    setBanner(null);

    // Header
    const p = item.progress || {};
    document.getElementById('hdr-batch').textContent = `batch ${p.batch || '–'}`;
    document.getElementById('hdr-progress').style.width = `${Math.round((p.batch_pct || 0) * 100)}%`;
    document.getElementById('hdr-pct').textContent = `${Math.round((p.batch_pct || 0) * 100)}%`;
    document.getElementById('hdr-done').textContent = `me: ${p.done_by_me || 0}`;

    // State
    const view = document.getElementById('state-view');
    if (item.state_format === 'json') {
        let doc;
        try {
            doc = JSON.parse(item.state);
        } catch (e) {
            doc = item.state;
        }
        view.className = 'json-view';
        view.innerHTML = renderJson(doc, '', 0);
        document.getElementById('state-meta').textContent = 'json';
    } else {
        view.className = 'state-text';
        view.innerHTML = tokenize(item.state, container({ side: 'state', pointer: null, text: item.state }));
        document.getElementById('state-meta').textContent = `text ${cpLength(item.state).toLocaleString()}c`;
    }
    document.getElementById('state-pane').scrollTop = 0;

    renderQuestion(item.question, qid);
    document.getElementById('question-asof').textContent = item.asof ? `as of ${item.asof}` : '';
    renderReasons();
    renderSpans();
    setRegion('state');
}

function container(info) {
    const id = `c${Object.keys(containers).length}`;
    containers[id] = info;
    return id;
}

// Words, punctuation and whitespace each get a span carrying their offsets, so a
// DOM selection maps back to exact character positions in the original string.
// Offsets count Unicode code points, as the server (Python) does, not the UTF-16
// units JS strings index by, so text with emoji or other astral characters
// still lines up.
function tokenize(text, cid) {
    const re = /(\s+)|([\p{L}\p{N}_]+(?:['’][\p{L}\p{N}_]+)*)|([^\s\p{L}\p{N}_])/gu;
    let html = '';
    let m;
    let cp = 0;
    while ((m = re.exec(text)) !== null) {
        const kind = m[1] ? 'ws' : (m[2] ? 'w' : 'p');
        const len = cpLength(m[0]);
        html += `<span class="tok ${kind}" data-c="${cid}" data-s="${cp}" data-e="${cp + len}">${escapeHtml(m[0])}</span>`;
        cp += len;
    }
    return html;
}

function cpLength(str) {
    let n = 0;
    for (const _ of str) n++;
    return n;
}

function codePoints(cid) {
    const c = containers[cid];
    if (!c.cps) c.cps = Array.from(c.text);
    return c.cps;
}

function escapePointerToken(key) {
    return String(key).replace(/~/g, '~0').replace(/\//g, '~1');
}

function bareToken(text, cls, info) {
    const cid = container({ ...info, text, bare: true });
    return `<span class="tok w ${cls}" data-c="${cid}" data-s="0" data-e="${cpLength(text)}">${escapeHtml(text)}</span>`;
}

// JSON states are pretty-printed for reading; every span is still stored as a
// pointer plus offsets into the original string value (FR-17, §6.3).
function renderJson(value, pointer) {
    if (value !== null && typeof value === 'object') {
        const entries = Array.isArray(value) ? value.map((v, i) => [String(i), v]) : Object.entries(value);
        const open = Array.isArray(value) ? '[' : '{', close = Array.isArray(value) ? ']' : '}';
        if (!entries.length) return `<span class="json-punct">${open}${close}</span>`;
        return entries.map(([k, v]) => {
            const ptr = `${pointer}/${escapePointerToken(k)}`;
            const key = Array.isArray(value)
                ? `<span class="json-punct">${escapeHtml(k)}</span>`
                : bareToken(k, 'json-key', { side: 'state', pointer: ptr, key: true });
            const nested = v !== null && typeof v === 'object';
            return `<div class="json-row">${key}<span class="json-punct">: </span>${
                nested ? `<div>${renderJson(v, ptr)}</div>` : renderJson(v, ptr)}</div>`;
        }).join('');
    }
    if (typeof value === 'string') {
        const cid = container({ side: 'state', pointer, text: value });
        return `<span class="json-string">${tokenize(value, cid)}</span>`;
    }
    return bareToken(JSON.stringify(value), 'json-scalar', { side: 'state', pointer });
}

function buildOptions(q) {
    const crit = q.criteria;
    if (q.type === 'choice') {
        return Object.entries(crit).map(([k, d], i) => ({ key: k, letter: String.fromCharCode(97 + i), name: k, desc: d }));
    }
    if (q.type === 'score') {
        return crit.map((d, i) => ({ key: String(i), letter: String.fromCharCode(97 + i), name: String(i), desc: d }));
    }
    const descs = crit || {};
    return ['true', 'false'].map(k => ({
        key: k, letter: k[0], name: k,
        desc: descs[k] ?? NOUL_DEFAULT[k],
        isDefault: descs[k] == null,  // a default isn't the item's text, so it takes no spans
    }));
}

function asJson(value) {
    return `<pre class="json-block">${escapeHtml(JSON.stringify(value, null, 2))}</pre>`;
}

function renderQuestion(q, qid) {
    document.getElementById('question-type').textContent = `(${q.type})`;
    const instr = q.instructions;
    // FR-3: a missing instructions field falls back to the qid; dicts and lists render as JSON
    document.getElementById('question-text').innerHTML =
        instr == null ? escapeHtml(qid) : (typeof instr === 'string' ? escapeHtml(instr) : asJson(instr));
    document.getElementById('options-view').innerHTML = L.options.map(o => {
        const selectable = text => tokenize(text, container({ side: 'option', option: o.key, text }));
        let name = escapeHtml(q.type === 'choice' ? o.name.replace(/_/g, ' ') : o.name);
        let desc = '';
        if (o.desc == null) desc = '';
        else if (typeof o.desc !== 'string') desc = asJson(o.desc);
        else if (o.desc === o.name) {
            // Key and description are the same text (sentence-like choice keys, e.g. piqa):
            // show it once, selectable, unless it's a slug that displays with spaces.
            if (/\s/.test(o.desc) || !o.desc.includes('_')) name = selectable(o.desc);
        }
        else if (o.isDefault) desc = `<span class="option-desc"><em>${escapeHtml(o.desc)}</em></span>`;
        else desc = `<span class="option-desc">${selectable(o.desc)}</span>`;
        return `<div class="option-row"><span class="option-key">${o.letter}</span>` +
               `<span class="option-name">${name}${desc ? ':' : ''}</span>${desc}</div>`;
    }).join('');
}

function renderReasons() {
    const rows = L.item.reason_set.map((r, i) => {
        const checked = L.reasons.has(r);
        const policy = (L.item.span_policy || {})[r];
        const tags = '<span class="tags">' + [CANDIDATE_REASONS.has(r) ? '<span class="tag cand">cand</span>' : '',
                      policy === 'required' ? '<span class="tag">*span</span>' : ''].join('') + '</span>';
        return `<li class="reason-row ${checked ? 'checked' : ''} ${L.active === r ? 'active' : ''}"
                    title="${escapeHtml(reasonDefs[r] || '')}"
                    onclick="labelAction(() => toggleReason('${r}'))">
                    <span class="label-key">${REASON_KEYS[i] || ''}</span>
                    <span>${checked ? '☑' : '☐'} ${escapeHtml(r)}</span>${tags}</li>`;
    });
    rows.push(`<li class="reason-row answerable ${L.answerable ? 'checked' : ''}"
                   onclick="labelAction(toggleAnswerable)">
                   <span class="label-key">Space</span><span>${L.answerable ? '◉' : '○'} answerable, no abstain</span></li>`);
    document.getElementById('reason-list').innerHTML = rows.join('');
}

function spanLabel(s) {
    const where = s.side === 'option' ? `option ${s.option}` : (s.pointer ? `state ${s.pointer}` : 'state');
    const opt = s.side === 'state' && s.option != null ? ` → ${escapeHtml(s.option)}` : '';
    const reasons = s.reasons.length ? ` [${s.reasons.map(escapeHtml).join(', ')}]` : '';
    return `${escapeHtml(where)} “${escapeHtml(s.text)}” <span class="role-chip ${s.role}">${s.role}</span>${opt}${reasons}`;
}

function renderSpans() {
    document.getElementById('span-list').innerHTML = L.spans.length
        ? L.spans.map((s, i) => `<div class="span-item ${i === L.focusedSpan ? 'focused' : ''}"
               onclick="focusSpan(${i})">#${i + 1} ${spanLabel(s)}</div>`).join('')
        : '<span class="option-desc">No spans yet. Select text, then press s / r / u / m.</span>';
    paintTokens();
}

function tokensOf(cid) {
    return document.querySelectorAll(`.tok[data-c="${cid}"]`);
}

function cidFor(span) {
    return Object.keys(containers).find(cid => {
        const c = containers[cid];
        if (c.side !== span.side) return false;
        if (span.side === 'option') return c.option === span.option;
        if (span.start == null) return c.pointer === span.pointer && c.bare && c.text === span.text;
        return (c.pointer ?? null) === (span.pointer ?? null) && !c.bare;
    });
}

function paintTokens() {
    document.querySelectorAll('.tok').forEach(t =>
        t.classList.remove('selected', 'role-support', 'role-refute', 'role-unsupported', 'role-framing', 'cursor'));
    const mark = (sel, cls) => {
        const cid = sel.cid || cidFor(sel);
        if (!cid) return;
        tokensOf(cid).forEach(t => {
            if (sel.start == null || (+t.dataset.s < sel.end && +t.dataset.e > sel.start)) t.classList.add(cls);
        });
    };
    L.spans.forEach(s => mark(s, `role-${s.role}`));
    if (L.selection) mark(L.selection, 'selected');
    const cur = regionTokens()[L.cursor];
    if (cur) cur.classList.add('cursor');
}

function setBanner(text) {
    const el = document.getElementById('mode-banner');
    el.textContent = text || '';
    el.classList.toggle('hidden', !text);
}

// ============================================================================
// Actions (each undoable with z)
// ============================================================================

function snapshot() {
    return JSON.stringify({ reasons: [...L.reasons], answerable: L.answerable, active: L.active,
                            spans: L.spans, focusedSpan: L.focusedSpan });
}

function labelAction(fn) {
    if (!L) return;
    const before = snapshot();
    fn();
    if (snapshot() !== before) L.history.push(before);
    renderReasons();
    renderSpans();
}

function undo() {
    if (!L || !L.history.length) return;
    const s = JSON.parse(L.history.pop());
    L.reasons = new Set(s.reasons);
    L.answerable = s.answerable;
    L.active = s.active;
    L.spans = s.spans;
    L.focusedSpan = s.focusedSpan;
    renderReasons();
    renderSpans();
}

function toggleReason(r) {
    if (L.reasons.has(r)) {
        L.reasons.delete(r);
        L.spans.forEach(s => { s.reasons = s.reasons.filter(x => x !== r); });
        if (L.active === r) L.active = orderedChecked()[0] || null;
    } else {
        L.reasons.add(r);
        L.answerable = false;
        L.active = r;  // §6.2: turning a reason on makes it the active reason
    }
}

function orderedChecked() {
    return L.item.reason_set.filter(r => L.reasons.has(r));
}

function toggleAnswerable() {
    L.answerable = !L.answerable;
    if (L.answerable) {
        L.reasons.clear();
        L.spans.forEach(s => { s.reasons = []; });
        L.active = null;
    }
}

function cycleActive(delta) {
    const checked = orderedChecked();
    if (!checked.length) return;
    const i = checked.indexOf(L.active);
    L.active = checked[(i + delta + checked.length) % checked.length];
}

function focusSpan(i) {
    L.focusedSpan = i;
    renderSpans();
}

function deleteFocusedSpan() {
    if (!L.spans.length) return;
    const i = L.focusedSpan >= 0 && L.focusedSpan < L.spans.length ? L.focusedSpan : L.spans.length - 1;
    L.spans.splice(i, 1);
    L.focusedSpan = Math.min(i, L.spans.length - 1);
}

function addSpan(role, option) {
    const sel = L.selection;
    const span = {
        side: sel.side, role, text: sel.text,
        option: sel.side === 'option' ? sel.option : option,
        pointer: sel.pointer ?? null,
        start: sel.start, end: sel.end,
        reasons: L.active ? [L.active] : [],
    };
    L.spans.push(span);
    L.focusedSpan = L.spans.length - 1;
    L.selection = null;
    L.anchor = -1;
}

// ============================================================================
// Selection: word-snapped by default, Alt for character precision (FR-18)
// ============================================================================

function selectionFromRange(startTok, startOffset, endTok, endOffset, precise) {
    const cid = startTok.dataset.c;
    if (endTok.dataset.c !== cid) {
        setBanner('A span must stay within one text: the state, one JSON string value or one option.');
        return null;
    }
    const info = containers[cid];
    if (info.bare) {
        return { cid, side: info.side, pointer: info.pointer, option: info.option, start: null, end: null, text: info.text };
    }
    let start, end;
    if (precise) {
        // DOM offsets are UTF-16 units inside the token; convert to code points
        start = +startTok.dataset.s + cpLength(startTok.textContent.slice(0, startOffset));
        end = +endTok.dataset.s + cpLength(endTok.textContent.slice(0, endOffset));
    } else {
        // Snap outward to whole words: "playi|ng a gui|tar" -> "playing a guitar"
        start = +startTok.dataset.s;
        end = +endTok.dataset.e;
    }
    const cps = codePoints(cid);
    while (start < end && /\s/.test(cps[start])) start++;
    while (end > start && /\s/.test(cps[end - 1])) end--;
    if (start >= end) return null;
    return { cid, side: info.side, pointer: info.pointer ?? null, option: info.option, start, end,
             text: cps.slice(start, end).join('') };
}

function tokenOfNode(node) {
    const el = node.nodeType === Node.TEXT_NODE ? node.parentElement : node;
    return el && el.closest ? el.closest('.tok') : null;
}

function handleMouseUp(e) {
    if (!L || L.mode) return;
    const sel = window.getSelection();
    const target = e.target.closest ? e.target.closest('.tok') : null;
    if (!sel || sel.isCollapsed) {
        if (target) {
            const info = containers[target.dataset.c];
            if (info && info.bare) {
                L.selection = selectionFromRange(target, 0, target, 0, false);
            } else {
                setRegion(target.closest('#options-view') ? 'options' : 'state');
                L.cursor = regionTokens().indexOf(target);
                L.selection = null;
            }
            paintTokens();
        }
        return;
    }
    const range = sel.getRangeAt(0);
    const a = tokenOfNode(range.startContainer), b = tokenOfNode(range.endContainer);
    if (!a || !b) return;
    const startOffset = range.startContainer.nodeType === Node.TEXT_NODE ? range.startOffset : 0;
    const endOffset = range.endContainer.nodeType === Node.TEXT_NODE ? range.endOffset : b.textContent.length;
    L.selection = selectionFromRange(a, startOffset, b, endOffset, e.altKey);
    sel.removeAllRanges();
    paintTokens();
}

function regionTokens() {
    const root = L && L.region === 'options' ? '#options-view' : '#state-view';
    return [...document.querySelectorAll(`${root} .tok.w`)];
}

function setRegion(region) {
    L.region = region;
    L.cursor = -1;
    L.anchor = -1;
    document.getElementById('state-pane').classList.toggle('focused', region === 'state');
    document.getElementById('question-pane').classList.toggle('focused', region === 'options');
}

function moveCursor(delta, extend) {
    const toks = regionTokens();
    if (!toks.length) return;
    const next = L.cursor < 0 ? (delta > 0 ? 0 : toks.length - 1) : Math.max(0, Math.min(toks.length - 1, L.cursor + delta));
    if (extend) {
        if (L.anchor < 0) L.anchor = L.cursor < 0 ? next : L.cursor;
        const a = toks[Math.min(L.anchor, next)], b = toks[Math.max(L.anchor, next)];
        if (a.dataset.c !== b.dataset.c) return;  // don't extend across texts
        L.selection = selectionFromRange(a, 0, b, b.textContent.length, false);
    } else {
        L.anchor = -1;
        L.selection = null;
    }
    L.cursor = next;
    paintTokens();
    toks[next].scrollIntoView({ block: 'nearest' });
}

// ============================================================================
// Submit, skip, flag
// ============================================================================

async function submitItem(override) {
    if (L.quiz && L.feedback) return nextQuizQuestion();  // onboarding.js
    const body = {
        item_id: L.item.item_id,
        answerable: L.answerable,
        reasons: orderedChecked(),
        note: document.getElementById('note').value,
        spans: L.spans.map(({ side, role, text, option, pointer, start, end, reasons }) =>
            ({ side, role, text, option, pointer, start, end, reasons })),
        policy_override: override,
        active_ms: Math.round(L.timer.active),
    };
    if (L.quiz) return submitQuizAnswer(body);  // onboarding.js
    const resp = await authenticatedFetch('/api/annotations', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    if (resp.ok) {
        showMessage('Saved', 'success');
        return loadNextItem();
    }
    const detail = (await resp.json()).detail;
    if (detail && detail.problems) {
        setBanner(detail.problems.join(' · ') + (detail.policy ? '  —  Shift+Enter saves anyway.' : ''));
    } else {
        setBanner(typeof detail === 'string' ? detail : 'Could not save');
    }
}

async function skipItem(code) {
    const resp = await authenticatedFetch('/api/skip', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ item_id: L.item.item_id, code, note: document.getElementById('note').value || null }),
    });
    if (resp.ok) {
        showMessage(`Skipped (${code})`, 'success');
        return loadNextItem();
    }
    setBanner((await resp.json()).detail || 'Could not skip');
}

async function flagItem(kind) {
    const resp = await authenticatedFetch('/api/flag', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ item_id: L.item.item_id, kind, note: document.getElementById('note').value || null }),
    });
    const data = await resp.json();
    setBanner(resp.ok ? null : data.detail);
    if (resp.ok) showMessage(`Flagged (${kind})`, 'success');
}

// ============================================================================
// Keyboard (§6.2). Returns true when the key was handled.
// ============================================================================

function labelKeyDown(e) {
    if (!L || document.getElementById('label-tab').classList.contains('hidden')) return false;
    const k = e.key;

    // Two-key modes: role -> option, skip -> code, flag -> kind
    if (L.mode) {
        e.preventDefault();
        if (k === 'Escape') {
            L.mode = null;
            setBanner(null);
            return true;
        }
        if (L.mode === 'role-option') {
            const opt = k === '-' ? { key: null } : L.options.find(o => o.letter === k.toLowerCase());
            if (opt) {
                L.mode = null;
                setBanner(null);
                labelAction(() => addSpan(L.pendingRole, opt.key));
            }
        } else if (L.mode === 'skip' && /^[1-5]$/.test(k)) {
            L.mode = null;
            setBanner(null);
            skipItem(SKIP_CODES[+k - 1]);
        } else if (L.mode === 'flag' && /^[1-3]$/.test(k)) {
            L.mode = null;
            flagItem(FLAG_KINDS[+k - 1]);
        }
        return true;
    }

    const idx = REASON_KEYS.indexOf(k);
    if (idx >= 0 && !e.ctrlKey && !e.metaKey && !e.altKey) {
        const r = L.item.reason_set[idx];
        if (r) labelAction(() => toggleReason(r));
        e.preventDefault();
        return true;
    }

    switch (k) {
        case ' ':
            labelAction(toggleAnswerable);
            break;
        case '[':
            labelAction(() => cycleActive(-1));
            break;
        case ']':
            labelAction(() => cycleActive(1));
            break;
        case 'ArrowLeft': case 'h':
            moveCursor(-1, e.shiftKey);
            break;
        case 'ArrowRight': case 'l':
            moveCursor(1, e.shiftKey);
            break;
        case 'H':
            moveCursor(-1, true);
            break;
        case 'L':
            moveCursor(1, true);
            break;
        case 'Tab':
            if (e.shiftKey || L.region === 'options') {
                if (L.region === 'options' && !e.shiftKey) {
                    document.getElementById('note').focus();
                } else {
                    setRegion(L.region === 'options' ? 'state' : 'options');
                }
            } else {
                setRegion('options');
            }
            paintTokens();
            break;
        case 's': case 'r': case 'u': case 'm': {
            if (!L.selection) {
                const cur = regionTokens()[L.cursor];
                if (cur) L.selection = selectionFromRange(cur, 0, cur, cur.textContent.length, false);
            }
            if (!L.selection) {
                setBanner('Select some text first (drag, or move with ← → and extend with Shift).');
                break;
            }
            const role = ROLES[k];
            if (role === 'unsupported' && L.selection.side !== 'option') {
                setBanner('unsupported spans go on option text, not the state.');
                break;
            }
            if (role === 'framing' && L.selection.side !== 'state') {
                setBanner('framing spans go on the state, not on an option.');
                break;
            }
            if (L.selection.side === 'option') {
                labelAction(() => addSpan(role, null));
            } else {
                L.pendingRole = role;
                L.mode = 'role-option';
                const letters = L.options.map(o => o.letter).join('/');
                setBanner(`${role}: which option is this span about? ${letters}, - for none, Esc cancels`);
            }
            break;
        }
        case 'Delete': case 'Backspace':
            labelAction(deleteFocusedSpan);
            break;
        case 'n':
            document.getElementById('note').focus();
            break;
        case 'Enter':
            submitItem(e.shiftKey);
            break;
        case 'x':
            if (L.quiz) break;  // the quiz has no skip
            L.mode = 'skip';
            setBanner('Skip: 1 cannot_judge · 2 broken_item · 3 offensive · 4 too_long · 5 other · Esc cancels');
            break;
        case 'f':
            if (L.quiz) break;
            L.mode = 'flag';
            setBanner('Flag: 1 bad_item · 2 guideline_unclear · 3 other · Esc cancels');
            break;
        case 'z':
            undo();
            break;
        case 'g':
            showGuideline(false);
            break;
        case 'Escape':
            L.selection = null;
            L.anchor = -1;
            setBanner(null);
            paintTokens();
            break;
        default:
            return false;
    }
    e.preventDefault();
    return true;
}

// ============================================================================
// Active time (FR-22): counts only while the tab is visible and the labeler
// interacted in the last 60 s.
// ============================================================================

function noteInteraction() {
    if (L) L.timer.lastInteraction = Date.now();
}

setInterval(() => {
    if (!L) return;
    const now = Date.now();
    const t = L.timer;
    if (document.visibilityState === 'visible' && now - t.lastInteraction < IDLE_MS) {
        t.active += now - t.last;
    }
    t.last = now;
    document.getElementById('hdr-active').textContent = `${Math.round(t.active / 1000)}s`;
}, 500);

document.addEventListener('visibilitychange', () => {
    if (L) L.timer.last = Date.now();  // hidden time never counts
});

document.addEventListener('DOMContentLoaded', () => {
    fetch('/api/reasons').then(r => r.json()).then(d => {
        d.reasons.forEach(r => { reasonDefs[r.key] = r.definition; });
        if (L) renderReasons();
    }).catch(() => {});
    ['keydown', 'mousedown', 'mousemove', 'wheel', 'input'].forEach(ev =>
        document.addEventListener(ev, noteInteraction, { passive: true }));
    document.getElementById('state-view').addEventListener('mouseup', handleMouseUp);
    document.getElementById('options-view').addEventListener('mouseup', handleMouseUp);
    const note = document.getElementById('note');
    note.addEventListener('keydown', e => {
        if (e.key === 'Escape') {
            note.blur();
            if (L) setRegion('state');
        } else if (e.key === 'Enter' && (e.ctrlKey || e.metaKey) && L) {
            e.preventDefault();
            submitItem(e.shiftKey);
        }
    });
});
