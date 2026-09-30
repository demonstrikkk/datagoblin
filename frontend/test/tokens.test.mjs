import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, relative, extname } from 'node:path';

/**
 * A design token is not a colour.
 *
 * Every token in `:root` is a bare RGB channel triplet — `--ok: 63 125 88` — so
 * that Tailwind can compile `bg-ok/25` into `rgb(var(--ok) / 0.25)`. The cost is
 * that `var(--ok)` on its own expands to `63 125 88`, which is not a valid CSS
 * colour, and the two failure modes are silent and opposite:
 *
 *   invalid CSS `background`  -> paints nothing
 *   invalid SVG `fill`        -> falls back to black
 *
 * That is not hypothetical. `StackedBar` painted an empty pill, so every
 * provenance bar in the product was blank, and `Treemap` rendered as a solid
 * black rectangle with unreadable text. Nothing errored, the build passed, and
 * the tables and prose around them looked correct — so the whole thing read as
 * "the UI is bad" rather than as a broken colour.
 *
 * Anything that needs a token as a whole colour must wrap it in `rgb()`, which
 * is what `toneVar` in lib/format.js exists for.
 */

const ROOTS = ['src'];
const EXTENSIONS = new Set(['.js', '.jsx', '.mjs', '.css']);

/* Tokens that are lengths, easings and shadows, not colours. Using one of these
   as a colour is a different mistake and this check does not claim to catch it. */
const NOT_A_COLOUR = new Set([
  'e-swift',
  'e-spring',
  'e-exit',
  'dur-1',
  'dur-2',
  'dur-3',
  'rail-w',
  'inspector-w',
  'shell-pad',
  'accent-ring',
  'paper-2',
  'paper-3',
]);

function walk(dir, out = []) {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      if (entry === 'node_modules' || entry === 'dist') continue;
      walk(full, out);
    } else if (EXTENSIONS.has(extname(entry))) {
      out.push(full);
    }
  }
  return out;
}

/** `--ok` from `var(--ok)`. */
const TOKEN = /var\(\s*--([a-z0-9-]+)\s*\)/gi;

const files = ROOTS.flatMap((d) => walk(d));
const failures = [];
let checked = 0;

/* Prose about the rule is not a violation of it, and the helper that exists to
   satisfy the rule is not itself a violation. Comments, docstrings and the body
   of `toneVar` are excluded. */
const isProse = (line) => {
  const t = line.trim();
  return (
    t.startsWith('*') ||
    t.startsWith('//') ||
    t.startsWith('/*') ||
    t.startsWith('export const toneVar')
  );
};

for (const file of files) {
  const text = readFileSync(file, 'utf8');
  checked += 1;

  text.split('\n').forEach((line, i) => {
    if (isProse(line)) return;

    // A token interpolated straight into var(), e.g. `var(--${tone})`. Correct
    // only when the rgb() wrapper is there, since the interpolation is how a
    // computed tone gets in.
    if (/var\(\s*--\$\{/.test(line) && !/rgb\(\s*var\(\s*--\$\{/.test(line)) {
      failures.push(
        `${relative('.', file)}:${i + 1}  interpolates a token straight into var() ` +
          `without rgb(); use toneVar()  |  ${line.trim().slice(0, 70)}`
      );
      return;
    }

    if (/rgb\(\s*var\(/.test(line)) return; // correct
    if (/\[var\(/.test(line)) return; // arbitrary-value: a length, not a colour
    TOKEN.lastIndex = 0;
    let m;
    while ((m = TOKEN.exec(line)) !== null) {
      const name = m[1];
      if (NOT_A_COLOUR.has(name)) continue;
      failures.push(
        `${relative('.', file)}:${i + 1}  var(--${name}) is a bare RGB triplet, not a ` +
          `colour — wrap it: rgb(var(--${name}))  |  ${line.trim().slice(0, 70)}`
      );
    }
  });
}

console.log('=== design tokens used as colours ===');
if (failures.length) {
  console.log(`  FAIL  ${failures.length} problem(s) in ${checked} files`);
  for (const f of failures) console.log(`    ${f}`);
  console.log('');
  process.exit(1);
}
console.log(`  ok  ${checked} files wrap every colour token in rgb()`);
console.log('');
