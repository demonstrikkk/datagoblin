// Executes the explainer's derivation logic outside a browser, so a broken
// reference or a bad regex shows up here instead of on camera.
import fs from 'node:fs';

const html = fs.readFileSync('index.html', 'utf8');

// Pull the inline module script (the last <script> block without src).
const blocks = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
if (!blocks.length) { console.error('FAIL: no inline script found'); process.exit(1); }
const src = blocks[blocks.length - 1];

// Keep only the declarations up to the DOM section, so we can call the
// pure functions without a document.
const cut = src.indexOf('// --- the motion player');
if (cut < 0) { console.error('FAIL: wiring marker missing'); process.exit(1); }
const pure = src.slice(0, cut);

const mod = await import('data:text/javascript;base64,' +
  Buffer.from(pure + '\nexport {detectEntity,detectFields,classifySector,deriveCost,buildTrace,gateTable,words,SECTORS};')
    .toString('base64'));

const PRESETS = [
  'Find 15 AI startups in London with founders and latest funding',
  'Find the top 10 stocks of the Indian share market with listing date and rank',
  'Identify active NGOs in Delhi working on women’s empowerment',
  'List AI companies in Europe that publish annual revenue',
  'open engineer jobs at Series A climate software companies in Berlin with salary',
  'sponsors of developer conferences in 2026',
];

let fail = 0;
const check = (cond, msg) => { if (!cond) { console.error('  FAIL: ' + msg); fail++; } };

for (const q of PRESETS) {
  const entity = mod.detectEntity(q);
  const fields = mod.detectFields(q);
  const sector = mod.classifySector(q, fields);
  const cost = mod.deriveCost(q, fields, entity.entity, sector);
  const trace = mod.buildTrace(q, fields, entity, sector, cost);
  const gate = mod.gateTable(q, fields, sector);

  console.log('\n' + q);
  console.log('   entity  :', entity.entity, '| fields:', fields.map(f => f.name).join(', '));
  console.log('   sector  :', sector.sector || `(none · ${sector.method})`,
              '| top3:', sector.scored.slice(0, 3).map(s => `${s.key}=${s.score}`).join(' '));
  console.log('   cost    :', cost.llmCalls, 'llm /', cost.webRequests, 'req /',
              cost.willBeJudged, 'judge /', cost.freeCells, 'free /',
              cost.bounded ? 'BUDGET CAPPED' : 'within budget');
  console.log('   trace   :', trace.length, 'stages |', 'gate rows:', gate.length);

  check(fields.length >= 1, 'at least one field');
  check(trace.length >= 9, 'full pipeline traced');
  check(gate.length === 6, 'six gate rows');
  check(typeof cost.note === 'string' && cost.note.length > 20, 'cost note written');
  check(trace.every(s => s.title && s.file), 'every stage names its module');
  // No stage may claim a stage the runner does not emit.
  const LEGAL = ['PLANNING','DISCOVERING','FETCHING','REDUCING','EXTRACTING','VALIDATING','NORMALIZE','DEDUPLICATING','FINALIZING','A GAP YOU WILL SEE'];
  check(trace.every(s => LEGAL.includes(s.title)), 'only real stages listed');
}

// --- the motion scene ------------------------------------------------------
//
// pipeline-motion.js only touches `document` from inside the Player, so
// buildScene can be exercised here with no DOM. The invariants worth pinning are
// the ones a reader would catch a lie in: the scene must be a pure function of
// the question, every token must be able to reach the end of its own path, and
// the gates that decide a value's fate must not overlap in time.

const motionSrc = fs.readFileSync('pipeline-motion.js', 'utf8');
const vm = await import('node:vm');
const vmCtx = vm.createContext({});
vm.runInContext(motionSrc, vmCtx);
const { buildScene, pathAt, samplePath } = vmCtx.PipelineMotion;

console.log('\n--- motion scene ---');

const QUERIES = [
  { q: PRESETS[0], fields: 4 },
  { q: PRESETS[4], fields: 7 },   // job-shaped: the widest schema
  { q: PRESETS[5], fields: 3 },
];

const STAGES = 9;
let mfail = 0;
const mcheck = (cond, msg) => { if (!cond) { console.error('  FAIL: ' + msg); mfail++; } };

for (const spec of QUERIES) {
  const f = mod.detectFields(spec.q);
  const e = mod.detectEntity(spec.q);
  const scene = buildScene({ fieldCount: f.length, entity: e.entity });
  const c = scene.counts;

  console.log(`   ${spec.q}`);
  console.log(`      tokens ${scene.tokens.length} | pops ${scene.pops.length} | ` +
              `verdicts ${c.green}+${c.red}+${c.amber} = ${c.green + c.red + c.amber} vs ${c.records} records`);

  mcheck(scene.windows.length === STAGES, `${STAGES} stage windows`);
  mcheck(scene.tokens.length > 40, 'scene has real traffic');
  mcheck(c.green + c.red + c.amber === c.records, 'every record gets exactly one verdict');
  mcheck(c.green > 0 && c.red > 0 && c.amber > 0, 'all three verdicts are represented');
  mcheck(c.rejected < c.candidates, 'discovery rejects some candidates');

  // Purity: same input, identical scene. A random element here would make the
  // scrubber lie, because scrubbing to t could show a run that never happened.
  const again = buildScene({ fieldCount: f.length, entity: e.entity });
  mcheck(JSON.stringify(again) === JSON.stringify(scene), 'buildScene is pure');

  // Monotone, non-overlapping windows: a token in two stages at once would mean
  // the run has two simultaneous owners.
  for (let i = 1; i < STAGES; i++) {
    mcheck(scene.windows[i].a >= scene.windows[i - 1].b - 1e-9,
      `stage ${i} starts after stage ${i - 1} ends`);
  }
  mcheck(scene.windows[0].a >= 0 && scene.windows[STAGES - 1].b <= 1,
    'timeline fits inside 0..1');

  // Every token is reachable: sampling the whole life of each path must produce
  // finite coordinates. A NaN here is a silently invisible token.
  let bad = 0;
  for (const t of scene.tokens) {
    for (let k = 0; k <= 10; k++) {
      const p = pathAt(t.sampled, k / 10);
      if (!Number.isFinite(p.x) || !Number.isFinite(p.y)) bad++;
    }
  }
  mcheck(bad === 0, 'every sampled point is finite');

  // Query shape must actually change the scene, or the animation is decoration.
  if (f.length > 1) {
    const narrow = buildScene({ fieldCount: 1, entity: 'item' });
    mcheck(narrow.counts.records !== c.records, 'wider schema yields more records');
    mcheck(narrow.counts.green + narrow.counts.red + narrow.counts.amber === narrow.counts.records,
      'narrow scene verdicts also balance');
  }
}

// A token must not double back on itself. Repeating a waypoint to fake a pause
// makes a Catmull-Rom spline overshoot, which is why the hold is applied to the
// clock instead -- so the geometry has to stay monotone on its own.
const back = buildScene({ fieldCount: 4, entity: 'company' });
let reversals = 0;
for (const t of back.tokens) {
  let prevY = -Infinity;
  for (let k = 0; k <= 30; k++) {
    const p = pathAt(t.sampled, k / 30);
    // Only the spine runs straight down; branches are allowed to rise.
    if (t.id.startsWith('spine') && p.y < prevY - 1e-6) reversals++;
    prevY = p.y;
  }
}
mcheck(reversals === 0, 'spine tokens never travel backwards');

// The hold is a property of time, so progress must stall at the node and resume.
const held = back.tokens.find(t => t.id === 'spine3');
const at = q => pathAt(held.sampled, held.u(held.a + (held.b - held.a) * q));
const arrive = at(0.3), held1 = at(0.45), held2 = at(0.6), gone = at(0.95);
mcheck(Math.abs(arrive.y - held1.y) < 0.01 && Math.abs(held1.y - held2.y) < 0.01,
  'token holds position at its stage');
mcheck(Math.abs(arrive.y - back.stageY[3]) < 0.5, 'the hold happens at the node, not beside it');
mcheck(Math.abs(arrive.x - (back.spine + 38)) < 0.5,
  'the query waits beside the glyph instead of covering it');
mcheck(gone.y > held2.y + 5, 'token leaves the stage afterwards');

console.log(fail || mfail
  ? `\nFAILURES: ${fail + mfail}`
  : '\nall checks passed');
process.exit(fail || mfail ? 1 : 0);
