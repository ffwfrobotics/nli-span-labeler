// E13 labeler front end. No build step, no external libraries (NFR-3).
// The auth, help-modal, tab and message helpers are carried over from the NLI
// span labeler's inline script (static/index.html at tag legacy-final).

// ============================================================================
// State
// ============================================================================
let currentUser = null;
let singleUser = false;

// Keyboard map, requirements §6.2. The help overlay is generated from this table,
// so it can't drift from what the handler does.
const SHORTCUTS = [
    { section: 'Reasons', rows: [
        ['1 … 9, 0', 'Toggle reason 1 … 10; turning one on makes it the active reason'],
        ['Space', 'Toggle answerable (clears reasons)'],
        ['[ ]', 'Cycle the active reason among the checked ones'],
    ]},
    { section: 'Spans', rows: [
        ['← → / h l', 'Move the word cursor; Shift extends the selection'],
        ['Tab', 'Move focus between state, options and note'],
        ['s r u m', 'Role support / refute / unsupported / framing, then a … z (t/f) for the option'],
        ['Del', 'Delete the focused span'],
        ['Alt + drag', 'Character-precise selection'],
    ]},
    { section: 'Item', rows: [
        ['Enter', 'Save and next'],
        ['Shift + Enter', 'Save, overriding the span policy'],
        ['x', 'Skip (then 1 … 5 for the reason code)'],
        ['f', 'Flag the item'],
        ['n', 'Focus the note (Esc leaves)'],
        ['z', 'Undo the last action'],
        ['?', 'This help'],
        ['g', 'Open the guideline'],
    ]},
];

// ============================================================================
// Authentication
// ============================================================================

async function checkAuthStatus() {
    try {
        const status = await (await fetch('/api/auth/status')).json();
        singleUser = status.single_user;

        const meResp = await fetch('/api/me');
        if (!meResp.ok) {
            showAuthModal();
            return false;
        }
        currentUser = await meResp.json();
        hideAuthModal();
        updateUserDisplay();
        return true;
    } catch (e) {
        console.error('Auth check failed:', e);
        showAuthModal();
        return false;
    }
}

function showAuthModal() {
    document.getElementById('auth-modal').classList.remove('hidden');
}

function hideAuthModal() {
    document.getElementById('auth-modal').classList.add('hidden');
}

async function handleLogin(event) {
    event.preventDefault();
    const login_name = document.getElementById('login-name').value;
    const password = document.getElementById('login-password').value;
    const errorEl = document.getElementById('login-error');

    try {
        const resp = await fetch('/api/auth/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ login_name, password })
        });
        if (!resp.ok) {
            const err = await resp.json();
            errorEl.textContent = err.detail || 'Login failed';
            errorEl.classList.remove('hidden');
            return;
        }
        errorEl.classList.add('hidden');
        if (await checkAuthStatus()) initializeApp();
    } catch (e) {
        errorEl.textContent = 'Connection error. Please try again.';
        errorEl.classList.remove('hidden');
    }
}

async function handleLogout() {
    try {
        await fetch('/api/auth/logout', { method: 'POST' });
        currentUser = null;
        showAuthModal();
        updateUserDisplay();
    } catch (e) {
        console.error('Logout failed:', e);
    }
}

function updateUserDisplay() {
    const userInfo = document.getElementById('user-info');
    if (!currentUser) {
        userInfo.classList.add('hidden');
        return;
    }
    document.getElementById('username-display').textContent = currentUser.pseudonym;
    userInfo.classList.remove('hidden');
    userInfo.querySelector('.logout-btn').classList.toggle('hidden', singleUser);
    updateAdminTabVisibility();
}

// ============================================================================
// Authenticated Fetch (handles 401s)
// ============================================================================

async function authenticatedFetch(url, options = {}) {
    const resp = await fetch(url, options);
    if (resp.status === 401) {
        currentUser = null;
        showAuthModal();
        updateUserDisplay();
        throw new Error('Session expired. Please log in again.');
    }
    return resp;
}

// ============================================================================
// Help Modal
// ============================================================================

function renderShortcuts() {
    const grid = document.getElementById('shortcuts-grid');
    grid.innerHTML = SHORTCUTS.map(section => `
        <div class="shortcuts-section">
            <h4>${section.section}</h4>
            ${section.rows.map(([keys, desc]) => `
                <div class="shortcut-row">
                    <span class="shortcut-keys">${keys.split(' ').map(k =>
                        ['…', '/', '+'].includes(k) ? k : `<span class="shortcut-key">${escapeHtml(k)}</span>`).join(' ')}</span>
                    <span class="shortcut-desc">${escapeHtml(desc)}</span>
                </div>`).join('')}
        </div>`).join('');
}

function showHelpModal() {
    document.getElementById('help-modal').classList.remove('hidden');
}

function hideHelpModal() {
    document.getElementById('help-modal').classList.add('hidden');
}

function toggleHelpModal() {
    const modal = document.getElementById('help-modal');
    modal.classList.contains('hidden') ? showHelpModal() : hideHelpModal();
}

function switchHelpTab(tabName) {
    document.querySelectorAll('#help-tabs .modal-tab').forEach(tab => tab.classList.remove('active'));
    event.target.classList.add('active');
    document.querySelectorAll('.help-tab-content').forEach(content => content.classList.remove('active'));
    document.getElementById(`help-${tabName}`).classList.add('active');
}

// ============================================================================
// Keyboard
// ============================================================================

function handleKeyDown(e) {
    // Don't handle keys when typing in inputs
    if (e.target.matches('input, textarea, select')) return;
    // Don't handle if the auth modal is shown
    if (!document.getElementById('auth-modal').classList.contains('hidden')) return;

    if (e.key === '?') {
        e.preventDefault();
        toggleHelpModal();
        return;
    }
    if (!document.getElementById('help-modal').classList.contains('hidden')) {
        if (e.key === 'Escape') hideHelpModal();
        return;
    }
    if (e.ctrlKey || e.metaKey) return;  // leave browser shortcuts alone
    // The labelling keys (§6.2) live in label.js
    if (typeof labelKeyDown === 'function') labelKeyDown(e);
}

// ============================================================================
// Tabs, messages
// ============================================================================

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text == null ? '' : String(text);
    return div.innerHTML;
}

function showMessage(text, type) {
    const area = document.getElementById('message-area');
    area.innerHTML = `<div class="message ${type}">${escapeHtml(text)}</div>`;
    setTimeout(() => { area.innerHTML = ''; }, 3000);
}

function switchTab(tabName) {
    document.querySelectorAll('.tab-content').forEach(el => el.classList.add('hidden'));
    document.querySelectorAll('.nav-tab').forEach(el => el.classList.remove('active'));
    document.getElementById(`${tabName}-tab`).classList.remove('hidden');
    document.querySelector(`.nav-tab[data-tab="${tabName}"]`).classList.add('active');
    if (tabName === 'admin') loadAdmin();
    if (tabName === 'label' && typeof loadNextItem === 'function' && !L) loadNextItem();
}

function isAdmin() {
    return currentUser && (currentUser.role === 'owner' || currentUser.role === 'admin');
}

function updateAdminTabVisibility() {
    document.querySelectorAll('.admin-only').forEach(el => el.classList.toggle('hidden', !isAdmin()));
}

// ============================================================================
// Admin
// ============================================================================

// Agreement dashboard (requirements §6.4, FR-37/38/41/42)
function fmt(x, digits = 2) {
    return x == null ? '–' : Number(x).toFixed(digits);
}

function agreementTable(section, highlightCandidates, candidates) {
    const rows = Object.entries(section.per_reason).map(([reason, d]) => [reason, d]);
    rows.push(['any abstain', section.any_abstain]);
    const body = rows.map(([reason, d]) => {
        const cand = highlightCandidates && candidates && candidates[reason];
        const notes = [];
        if (cand && cand.below_min) notes.push('<span class="tag cand">BELOW min established</span>');
        else if (cand && cand.alpha != null && cand.min != null) notes.push('within range');
        if (d.unstable && d.n_items) notes.push('<span class="tag cand">! α unstable</span>');
        return `<tr class="${cand ? 'candidate-row' : ''}">
            <td>${escapeHtml(reason)}${cand ? ' <span class="tag cand">CAND</span>' : ''}</td>
            <td>${d.n_items}</td><td><strong>${fmt(d.alpha)}</strong></td>
            <td>${d.ci95 ? `${fmt(d.ci95[0])}–${fmt(d.ci95[1])}` : '–'}</td>
            <td>${d.prevalence == null ? '–' : Math.round(d.prevalence * 100) + '%'}</td>
            <td>${d.n_positive}</td><td>${notes.join(' ')}</td></tr>`;
    }).join('');
    return `<table class="admin-table"><thead><tr><th>reason</th><th>n</th><th>α</th><th>CI95</th><th>prev</th>
        <th>pos</th><th>notes</th></tr></thead><tbody>${body}</tbody></table>`;
}

function renderAgreement(rep) {
    const inter = rep.inter_rater;
    const cands = inter.candidates || {};
    const ref = Object.values(cands)[0];
    document.getElementById('agreement-meta').textContent =
        `${inter.n_items_pairable} items with 2+ human labels · labelers ${inter.labelers.join(', ') || '–'}` +
        (ref ? ` · established α min ${fmt(ref.min)} / median ${fmt(ref.median)}` : '');
    let html = `<h4 class="pane-title">Inter-rater (blind batches, gold excluded)</h4>` +
        agreementTable(inter, true, cands);
    if (rep.intra_rater.n_pairs) {
        html += `<h4 class="pane-title" style="margin-top: 14px;">Intra-rater: re-label vs first pass
                 (${rep.intra_rater.n_pairs} pairs; consistency, not agreement)</h4>` + agreementTable(rep.intra_rater);
    }
    for (const [model, d] of Object.entries(rep.human_vs_model)) {
        html += `<details style="margin-top: 10px;"><summary>Human vs ${escapeHtml(model)} (${d.n_pairs} pairs)</summary>
                 ${agreementTable(d)}</details>`;
    }
    for (const [pair, d] of Object.entries(rep.model_vs_model)) {
        html += `<details style="margin-top: 10px;"><summary>${escapeHtml(pair.replace('|', ' vs '))}
                 (${d.n_pairs} items)</summary>${agreementTable(d)}</details>`;
    }
    document.getElementById('agreement-view').innerHTML = html;
}

async function runExport() {
    const el = document.getElementById('export-result');
    el.textContent = 'Exporting…';
    const resp = await authenticatedFetch('/api/admin/export', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}),
    });
    const data = await resp.json();
    el.textContent = resp.ok
        ? `Written to ${data.directory}: ` + data.files.map(f => `${f.name} (${f.rows})`).join(', ')
        : `Export failed: ${JSON.stringify(data.detail)}`;
}

async function loadAdmin() {
    authenticatedFetch('/api/admin/agreement?n_boot=1000').then(r => r.json()).then(renderAgreement)
        .catch(e => showMessage(e.message, 'error'));
    try {
        const [labelers, flags, batches] = await Promise.all([
            authenticatedFetch('/api/admin/labelers').then(r => r.json()),
            authenticatedFetch('/api/admin/flags').then(r => r.json()),
            authenticatedFetch('/api/admin/batches').then(r => r.json()),
        ]);
        document.querySelector('#batches-table tbody').innerHTML = batches.batches.map(b => `
            <tr><td>${escapeHtml(b.name)}</td><td>${escapeHtml(b.status)}</td><td>${b.n_items}</td>
                <td>${b.relabel_of ? '–' : b.overlap_target}</td>
                <td>${b.reliability_subset ? `${b.reliability_subset} items × ${b.reliability_overlap}` : '–'}</td>
                <td>${b.relabel_of ? `${escapeHtml(b.relabel_of)} (after ${b.relabel_after_days}d)` : '–'}</td>
                <td>${escapeHtml(b.tier_ceiling)}</td></tr>`).join('')
            || '<tr><td colspan="7">No batches yet: python -m e13_labeler import FILE --batch NAME</td></tr>';
        document.querySelector('#labelers-table tbody').innerHTML = labelers.labelers.map(l => `
            <tr><td>${escapeHtml(l.pseudonym)}</td><td>${escapeHtml(l.login_name || '')}</td>
                <td>${escapeHtml(l.kind)}</td><td>${escapeHtml(l.role)}</td><td>${escapeHtml(l.clearance)}</td>
                <td>${escapeHtml(l.status)}</td><td>${escapeHtml(l.last_seen || '')}</td></tr>`).join('');
        document.querySelector('#flags-table tbody').innerHTML = flags.flags.map(f => `
            <tr><td>${escapeHtml(f.item_id)}</td><td>${escapeHtml(f.kind)}</td><td>${escapeHtml(f.note || '')}</td>
                <td>${escapeHtml(f.pseudonym)}</td><td>${escapeHtml(f.created_at)}</td></tr>`).join('')
            || '<tr><td colspan="5">No pending flags</td></tr>';
    } catch (e) {
        showMessage(e.message, 'error');
    }
}

// ============================================================================
// Startup
// ============================================================================

function initializeApp() {
    updateAdminTabVisibility();
    loadNextItem();
}

document.addEventListener('DOMContentLoaded', async () => {
    renderShortcuts();
    document.addEventListener('keydown', handleKeyDown);
    if (await checkAuthStatus()) initializeApp();
});
