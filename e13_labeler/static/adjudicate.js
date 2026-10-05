// Adjudication (FR-43, §6.5): labels side by side as L-a, L-b, ...; the
// admin picks the final reasons and spans, and may promote the result to gold.
// Saved as a new version beside the raw labels, which α keeps using.

let ADJ = null;  // the item being adjudicated

async function loadAdjudicationQueue() {
    const card = document.getElementById('adjudication-card');
    if (currentUser.clearance !== 'internal') return card.classList.add('hidden');
    const resp = await authenticatedFetch('/api/admin/adjudication');
    if (!resp.ok) return card.classList.add('hidden');
    const data = await resp.json();
    card.classList.remove('hidden');
    document.getElementById('adjudication-meta').textContent = `${data.count} item(s) with disagreements`;
    document.querySelector('#adjudication-table tbody').innerHTML = data.items.slice(0, 50).map(i => `
        <tr><td>${escapeHtml(i.item_id)}</td>
            <td><strong>${i.n_disagreements}</strong>: ${escapeHtml(i.disagree_on.join(', '))}</td>
            <td>${i.n_labels}</td><td>${escapeHtml(i.batches.join(', '))}</td>
            <td><button class="btn btn-small" onclick="openAdjudication('${encodeURIComponent(i.item_id)}')">Adjudicate</button></td></tr>`
    ).join('') || '<tr><td colspan="5">Nothing to adjudicate</td></tr>';
}

function adjSpanText(s) {
    const where = s.side === 'option' ? `option ${s.option}` : (s.pointer ? `state ${s.pointer}` : 'state');
    return `<span class="role-chip ${s.role}">${s.role}</span> ${escapeHtml(where)} “${escapeHtml(s.text)}”` +
        (s.option != null && s.side === 'state' ? ` → ${escapeHtml(s.option)}` : '') +
        (s.reasons.length ? ` [${escapeHtml(s.reasons.join(', '))}]` : '');
}

async function openAdjudication(encodedId) {
    const resp = await authenticatedFetch(`/api/admin/adjudication/${encodedId}`);
    const d = await resp.json();
    if (!resp.ok) return showMessage(d.detail || 'Could not load', 'error');
    ADJ = d;
    const prior = d.adjudication;
    const n = d.labels.length;
    const keys = ['answerable', ...d.reason_set];
    const has = (l, k) => k === 'answerable' ? l.answerable : l.reasons.includes(k);
    // Prefill: the previous adjudication, else the majority (ties unchecked)
    const initial = k => prior ? (k === 'answerable' ? prior.answerable : prior.reasons.includes(k))
                               : d.labels.filter(l => has(l, k)).length * 2 > n;
    const rows = keys.map(k => `<tr class="${d.disagree_on.includes(k) ? 'candidate-row' : ''}">
        <td>${escapeHtml(k)}</td>${d.labels.map(l => `<td>${has(l, k) ? '✕' : '·'}</td>`).join('')}
        <td><input type="checkbox" class="adj-final" data-key="${k}" ${initial(k) ? 'checked' : ''}
                   aria-label="final ${escapeHtml(k)}"></td></tr>`).join('');
    const spanKey = s => JSON.stringify([s.side, s.pointer, s.option, s.start, s.end, s.role]);
    const priorSpans = new Set((prior ? prior.spans : []).map(spanKey));
    const spans = d.labels.flatMap(l => l.spans.map(s => ({ ...s, who: l.labeler })));
    ADJ.spanList = spans;
    const spanRows = spans.map((s, i) => `<label class="adj-span"><input type="checkbox" class="adj-span-pick"
        data-i="${i}" ${priorSpans.has(spanKey(s)) ? 'checked' : ''}> <strong>${s.who}</strong> ${adjSpanText(s)}</label>`).join('');
    const notes = d.labels.filter(l => l.note).map(l => `<div><strong>${l.labeler}:</strong> “${escapeHtml(l.note)}”</div>`).join('');
    document.getElementById('adj-title').textContent = `Adjudicate ${d.item_id}`;
    document.getElementById('adj-body').innerHTML = `
        <div class="question-type">Disagreements: ${escapeHtml(d.disagree_on.join(', '))}${prior ? ` · adjudicated v${prior.version} by ${escapeHtml(prior.adjudicator)}` : ''}${d.gold ? ' · already gold' : ''}</div>
        <div class="adj-grid">
            <div><div class="pane-title"><span>State</span></div><pre class="adj-state">${escapeHtml(d.state)}</pre>
                 <div class="pane-title"><span>Question</span></div><pre class="adj-state">${escapeHtml(JSON.stringify(d.question, null, 2))}</pre></div>
            <div><table class="admin-table"><thead><tr><th>reason</th>${d.labels.map(l => `<th>${l.labeler}</th>`).join('')}<th>final</th></tr></thead>
                 <tbody>${rows}</tbody></table>${notes ? `<div class="dash-line">${notes}</div>` : ''}</div>
        </div>
        <div class="pane-title" style="margin-top: 10px;"><span>Spans: tick the ones to keep</span></div>
        <div>${spanRows || '<span class="option-desc">No spans</span>'}</div>
        <div class="form-group"><label for="adj-note">Note</label><textarea id="adj-note" class="note-textarea" rows="2">${escapeHtml(prior && prior.note || '')}</textarea></div>
        <div class="form-group"><label for="adj-explanation">Gold explanation (when promoting)</label>
            <input type="text" id="adj-explanation" value="${escapeHtml(d.gold && d.gold.explanation || '')}"></div>
        <div id="adj-error" class="form-error hidden"></div>
        <div class="modal-actions">
            <button class="btn" onclick="saveAdjudication(false)">Save adjudication</button>
            <button class="btn btn-primary" onclick="saveAdjudication(true)">Save + promote to gold</button>
        </div>`;
    document.getElementById('adjudication-modal').classList.remove('hidden');
}

function hideAdjudication() {
    document.getElementById('adjudication-modal').classList.add('hidden');
    ADJ = null;
}

async function saveAdjudication(promote) {
    const finals = [...document.querySelectorAll('.adj-final:checked')].map(el => el.dataset.key);
    const answerable = finals.includes('answerable');
    const reasons = finals.filter(k => k !== 'answerable');
    const seen = new Set();
    const spans = [...document.querySelectorAll('.adj-span-pick:checked')].map(el => ADJ.spanList[+el.dataset.i])
        .map(({ who, ...s }) => ({ ...s, reasons: s.reasons.filter(r => reasons.includes(r)) }))
        .filter(s => { const k = JSON.stringify(s); return !seen.has(k) && seen.add(k); });
    const resp = await authenticatedFetch(`/api/admin/adjudication/${encodeURIComponent(ADJ.item_id)}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ answerable, reasons, spans, promote,
                               note: document.getElementById('adj-note').value || null,
                               explanation: document.getElementById('adj-explanation').value || null }),
    });
    const data = await resp.json();
    if (!resp.ok) {
        const el = document.getElementById('adj-error');
        el.textContent = data.detail && data.detail.problems ? data.detail.problems.join(' · ') : String(data.detail);
        return el.classList.remove('hidden');
    }
    showMessage(`Adjudication v${data.adjudication.version} saved${promote ? ' and promoted to gold' : ''}`, 'success');
    hideAdjudication();
    loadAdjudicationQueue();
}
