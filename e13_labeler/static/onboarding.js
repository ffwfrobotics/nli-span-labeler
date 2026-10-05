// Onboarding: the guideline (FR-26), the quiz with immediate feedback (FR-27)
// and the retraining quiz after an auto-pause (FR-30). The quiz reuses the
// labelling screen (label.js) with L.quiz set: Enter answers, then Enter again
// moves on after the feedback.

let onboardingState = null;

// Returns true when onboarding took over the screen (so the caller doesn't load an item).
function showOnboarding(detail) {
    if (!currentUser || (currentUser.status === 'active' && !detail)) return false;
    refreshOnboarding();
    return true;
}

async function refreshOnboarding() {
    const resp = await authenticatedFetch('/api/onboarding');
    if (!resp.ok) return showEmpty('Could not load your onboarding status.');
    onboardingState = await resp.json();
    const st = onboardingState;
    if (st.next === 'label') return loadNextItem();
    if (st.next === 'wait') {
        return showEmpty(st.pause_reason === 'quiz_failed' || st.pause_reason === 'review'
            ? 'Your quiz results have gone to the project owner for review. They will be in touch.'
            : `Your account is ${st.status}${st.pause_reason ? ` (${st.pause_reason})` : ''}. Ask the project owner.`);
    }
    if (st.next === 'guideline') return showGuideline(true);
    return startQuiz();
}

// ============================================================================
// Guideline
// ============================================================================

function exampleHtml(ex, kind) {
    return `<div class="guideline-example ${kind}">
        <div class="example-kind">${kind === 'positive' ? 'Example' : 'Near miss'}: <strong>${escapeHtml(ex.verdict)}</strong></div>
        <div class="example-state">${escapeHtml(ex.state)}</div>
        <div class="example-question">Q: ${escapeHtml(ex.question)}</div>
        <div class="example-why">${escapeHtml(ex.why)}</div></div>`;
}

async function showGuideline(gate) {
    const resp = await authenticatedFetch('/api/guideline');
    if (!resp.ok) return;
    const g = await resp.json();
    const reasons = g.reasons.map((r, i) => `
        <section class="guideline-reason">
            <h4><span class="label-key">${(i + 1) % 10}</span> ${escapeHtml(r.key)}${r.candidate ? ' <span class="tag cand">candidate</span>' : ''}</h4>
            <p>${escapeHtml(r.definition)}</p>
            <p class="span-rule"><strong>Spans:</strong> ${escapeHtml(r.span_rule)}</p>
            <div class="guideline-examples">${exampleHtml(r.positive, 'positive')}${exampleHtml(r.negative, 'negative')}</div>
        </section>`).join('');
    document.getElementById('guideline-body').innerHTML = `
        <p class="question-type">Version ${escapeHtml(g.version)}${g.status ? ' · ' + escapeHtml(g.status) : ''}</p>
        ${renderMarkdown(g.intro)}${renderMarkdown(g.spans)}${reasons}`;
    const start = document.getElementById('guideline-start');
    start.classList.toggle('hidden', !gate);
    start.textContent = onboardingState && onboardingState.retake ? 'Retake the quiz' : 'Start the quiz';
    document.getElementById('guideline-modal').classList.remove('hidden');
    if (gate) showEmpty('Read the guideline, then start the quiz.');
}

function hideGuideline() {
    document.getElementById('guideline-modal').classList.add('hidden');
}

// ============================================================================
// Quiz
// ============================================================================

async function startQuiz() {
    hideGuideline();
    const resp = await authenticatedFetch('/api/quiz/start', { method: 'POST' });
    if (!resp.ok) return showEmpty((await resp.json()).detail || 'The quiz is not available yet.');
    return nextQuizQuestion();
}

async function nextQuizQuestion() {
    const resp = await authenticatedFetch('/api/quiz');
    const q = await resp.json();
    if (!resp.ok) return showEmpty(q.detail || 'No quiz.');
    if (q.finished) return showQuizResult(q);
    renderItem({ ...q.item, progress: { batch: `${q.kind} quiz`, batch_pct: q.position / q.total,
                                        done_by_me: q.position } });
    L.quiz = q;
    L.feedback = null;
    document.getElementById('quiz-feedback').classList.add('hidden');
    setBanner(`Quiz question ${q.position + 1} of ${q.total}. Answer as you would label; Enter submits.`);
}

async function submitQuizAnswer(body) {
    const resp = await authenticatedFetch('/api/quiz/answer', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ item_id: body.item_id, answerable: body.answerable, reasons: body.reasons,
                               spans: body.spans }),
    });
    const data = await resp.json();
    if (!resp.ok) return setBanner(typeof data.detail === 'string' ? data.detail : 'Could not submit');
    L.feedback = data;
    const gold = data.gold;
    const goldAnswer = gold.answerable ? 'answerable' : gold.reasons.join(', ');
    const spans = gold.spans.map(s => `<li><span class="role-${s.role}">${escapeHtml(s.role)}</span>
        “${escapeHtml(s.text)}”${s.option ? ` (option ${escapeHtml(s.option)})` : ''}</li>`).join('');
    const alts = Object.entries(gold.alternatives || {}).map(([r, a]) => `${r}: ${a.join(' / ')}`).join('; ');
    const el = document.getElementById('quiz-feedback');
    el.innerHTML = `<div class="quiz-verdict ${data.correct ? 'ok' : 'bad'}">${data.correct ? '✓ Matches gold' : '✗ Differs from gold'}</div>
        <div><strong>Gold:</strong> ${escapeHtml(goldAnswer)}${alts ? ` <span class="question-type">(also accepted: ${escapeHtml(alts)})</span>` : ''}</div>
        ${data.missed.length ? `<div><strong>Missed:</strong> ${escapeHtml(data.missed.join(', '))}</div>` : ''}
        ${data.extra.length ? `<div><strong>Not in gold:</strong> ${escapeHtml(data.extra.join(', '))}</div>` : ''}
        ${spans ? `<div><strong>Gold spans:</strong><ul>${spans}</ul></div>` : ''}
        ${gold.explanation ? `<div class="example-why">${escapeHtml(gold.explanation)}</div>` : ''}
        <div class="question-type">Enter: ${data.result ? 'see your result' : 'next question'}</div>`;
    el.classList.remove('hidden');
    el.scrollIntoView({ block: 'nearest' });
    setBanner(null);
    if (data.result) L.quiz.result = data.result;
}

async function showQuizResult(r) {
    document.getElementById('quiz-feedback').classList.add('hidden');
    const pct = Math.round(r.accuracy * 100);
    let text;
    if (r.passed) {
        text = `Passed (${pct}%). You can start labelling.`;
    } else if (r.retake || r.status === 'onboarding') {
        text = `Not passed (${pct}%; 75% needed${r.missed_too_often.length ? `, and ${r.missed_too_often.join(', ')} missed more than once` : ''}). Re-read the guideline, then retake the quiz once.`;
    } else {
        text = `Not passed (${pct}%). Your results have gone to the project owner for review.`;
    }
    showEmpty(text);
    if (await checkAuthStatus()) {
        if (r.passed) setTimeout(() => loadNextItem(), 1500);
        else if (currentUser.status !== 'paused' || currentUser.pause_reason === 'gold_accuracy') {
            setTimeout(() => refreshOnboarding(), 2500);
        }
    }
}
