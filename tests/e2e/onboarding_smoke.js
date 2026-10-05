// Browser smoke test for onboarding (FR-51, FR-60, FR-26, FR-27). Not run by
// pytest: it needs a running server and Chromium. A seed script writes a JSON
// file with an invite token and the gold answers ({token, gold: {item_id: [reasons]}, reasons}).
//   node tests/e2e/onboarding_smoke.js http://127.0.0.1:8766 seed.json /tmp
// Checks: invite link -> register -> contributor agreement -> guideline gate ->
// quiz with feedback after each answer -> pass -> the labelling screen; no JS errors.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const fs = require('fs');
const [BASE, SEED, OUT] = process.argv.slice(2);
const seed = JSON.parse(fs.readFileSync(SEED, 'utf8'));
const KEYS = ['1', '2', '3', '4', '5', '6', '7', '8', '9', '0'];

(async () => {
  const browser = await chromium.launch(process.env.CHROMIUM ? { executablePath: process.env.CHROMIUM } : {});
  const page = await browser.newPage({ viewport: { width: 1366, height: 768 } });
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('console', m => { if (m.type() === 'error' && !/status of 40[34]/.test(m.text())) errors.push(m.text()); });

  await page.goto(`${BASE}/?invite=${seed.token}`);
  await page.fill('#register-name', 'smoke-labeler');
  await page.fill('#register-password', 'smoke-password-1');
  await page.click('#register-form button[type=submit]');
  await page.waitForSelector('#agreement-modal:not(.hidden)');
  await page.screenshot({ path: `${OUT}/onb-1-agreement.png` });
  await page.click('#agreement-accept');
  await page.waitForSelector('#guideline-modal:not(.hidden)');
  console.log('guideline sections', await page.$$eval('.guideline-reason', els => els.length));
  await page.screenshot({ path: `${OUT}/onb-2-guideline.png` });
  await page.click('#guideline-start');

  for (let n = 0; n < 12; n++) {
    await page.waitForFunction(() => L && L.quiz && !L.feedback);
    const id = await page.evaluate(() => L.item.item_id);
    const reasons = seed.gold[id];
    if (!reasons.length) await page.keyboard.press('Space');
    for (const r of reasons) await page.keyboard.press(KEYS[seed.reasons.indexOf(r)]);
    await page.keyboard.press('Enter');
    await page.waitForSelector('#quiz-feedback:not(.hidden)');
    if (n === 0) await page.screenshot({ path: `${OUT}/onb-3-feedback.png` });
    const verdict = await page.textContent('.quiz-verdict');
    if (!verdict.includes('Matches')) errors.push(`question ${n} (${id}): ${verdict}`);
    await page.keyboard.press('Enter');
  }
  await page.waitForFunction(() => document.getElementById('label-empty-text').textContent.includes('Passed'));
  console.log('result:', await page.textContent('#label-empty-text'));
  await page.waitForFunction(() => L && !L.quiz, null, { timeout: 5000 });
  console.log('now labelling', await page.evaluate(() => L.item.item_id));
  await page.screenshot({ path: `${OUT}/onb-4-labelling.png` });
  console.log('errors:', JSON.stringify(errors));
  await browser.close();
  process.exit(errors.length ? 1 : 0);
})();
