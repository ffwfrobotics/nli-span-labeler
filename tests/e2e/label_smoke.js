// Browser smoke test for the labelling screen. Not run by pytest (it needs a
// running server and Chromium). To run it:
//   E13_DB=/tmp/e13-ui.db uv run python -m e13_labeler create-owner --login owner
//   E13_DB=/tmp/e13-ui.db uv run python -m e13_labeler import tests/fixtures/synthetic_pool.jsonl --batch pilot
//   E13_DB=/tmp/e13-ui.db uv run python -m e13_labeler batch open pilot
//   E13_DB=/tmp/e13-ui.db SINGLE_USER=1 uv run python -m e13_labeler serve --port 8765 &
//   node tests/e2e/label_smoke.js /tmp      # screenshots land in the given directory
// Checks: the screen fits 1366x768, word-snap and Alt char-precise selection,
// span roles and options, the conflicting_evidence policy, active time, and no JS errors.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const SP = process.argv[2];
(async () => {
  const browser = await chromium.launch(process.env.CHROMIUM ? { executablePath: process.env.CHROMIUM } : {});
  const page = await browser.newPage({ viewport: { width: 1366, height: 768 } });
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  await page.goto('http://127.0.0.1:8765/');
  await page.waitForSelector('#label-main:not(.hidden)');
  // Skip forward until a text item with prose (bbc) or fever shows up
  for (let i = 0; i < 8; i++) {
    const id = await page.evaluate(() => L.item.item_id);
    if (id.startsWith('bbc_news')) break;
    await page.keyboard.press('Space'); await page.keyboard.press('Enter'); await page.waitForTimeout(400);
  }
  console.log('item', await page.evaluate(() => L.item.item_id));
  const box = await page.evaluate(() => document.getElementById('label-footer').getBoundingClientRect().bottom);
  console.log('footer bottom', box, 'scrollHeight', await page.evaluate(() => document.documentElement.scrollHeight));
  // Mouse drag from the middle of "settles" to the middle of "disclosure": word snap
  const toks = await page.$$('#state-view .tok.w');
  const a = await toks[3].boundingBox(), b = await toks[4].boundingBox();
  console.log('tokens', await toks[3].textContent(), await toks[4].textContent());
  await page.mouse.move(a.x + a.width / 2, a.y + a.height / 2); await page.mouse.down();
  await page.mouse.move(b.x + b.width / 2, b.y + b.height / 2, { steps: 5 }); await page.mouse.up();
  console.log('snapped:', JSON.stringify(await page.evaluate(() => L.selection && [L.selection.text, L.selection.start, L.selection.end])));
  // Alt drag: char precise
  await page.keyboard.down('Alt');
  await page.mouse.move(a.x + a.width / 2, a.y + a.height / 2); await page.mouse.down();
  await page.mouse.move(b.x + b.width / 2, b.y + b.height / 2, { steps: 5 }); await page.mouse.up();
  await page.keyboard.up('Alt');
  console.log('alt:', JSON.stringify(await page.evaluate(() => L.selection && [L.selection.text, L.selection.start, L.selection.end])));
  await page.keyboard.press('3');  // conflicting_evidence
  await page.keyboard.press('s'); await page.keyboard.press('a');
  // option-side span: drag on the option description? bbc options have desc == name, so none. Select state word for refute
  await page.mouse.click(b.x + 2, b.y + 2);
  await page.keyboard.press('Shift+ArrowRight');
  await page.keyboard.press('r'); await page.keyboard.press('a');
  await page.waitForTimeout(1500);
  console.log('spans', JSON.stringify(await page.evaluate(() => L.spans.map(s => [s.role, s.text, s.option, s.reasons]))));
  console.log('active ms', await page.evaluate(() => Math.round(L.timer.active)));
  await page.screenshot({ path: SP + '/shot4.png' });
  await page.keyboard.press('Enter'); await page.waitForTimeout(500);
  console.log('after submit item', await page.evaluate(() => L && L.item.item_id), 'banner', await page.textContent('#mode-banner'));
  // help overlay
  await page.keyboard.press('?'); await page.waitForTimeout(200);
  await page.screenshot({ path: SP + '/shot5.png' });
  console.log('errors:', JSON.stringify(errors));
  await browser.close();
})();
