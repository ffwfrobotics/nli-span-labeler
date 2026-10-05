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
    if (e.key === 'Escape') {
        hideHelpModal();
        return;
    }
    // The labelling keys (§6.2) attach here with the labelling screen (M1).
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

async function loadAdmin() {
    try {
        const [labelers, flags] = await Promise.all([
            authenticatedFetch('/api/admin/labelers').then(r => r.json()),
            authenticatedFetch('/api/admin/flags').then(r => r.json()),
        ]);
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
}

document.addEventListener('DOMContentLoaded', async () => {
    renderShortcuts();
    document.addEventListener('keydown', handleKeyDown);
    if (await checkAuthStatus()) initializeApp();
});
