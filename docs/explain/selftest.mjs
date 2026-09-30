// Executes the explainer's derivation logic outside a browser, so a broken
// reference or a bad regex shows up here instead of on camera.
import fs from 'node:fs';

const html = fs.readFileSync('index.html', 'utf8');

// Pull the inline module script (the last <script> block without src).
const blocks = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
if (!blocks.length) { console.error('FAIL: no inline script found'); process.exit(1); }
const src = blocks[blocks.length - 1];

// Keep only the declarations up to the wiring section, so we can call the
// pure functions without a DOM.
const cut = src.indexOf('// --- wiring');
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

console.log('\n' + (fail ? `FAILURES: ${fail}` : 'all checks passed'));
process.exit(fail ? 1 : 0);
