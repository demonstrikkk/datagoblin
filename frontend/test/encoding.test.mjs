import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, relative, extname } from 'node:path';

/**
 * The source tree must be valid UTF-8.
 *
 * A previous pass repaired the corrupted UI strings in DatasetPage.jsx, but the
 * arrows and ticks were missed, because the detector looked for a Latin-1 lead
 * byte immediately followed by a C1 control — and the damage here is
 * cp1250-mangled, not Latin-1, so the byte after the lead is U+0153 and U+2020,
 * neither of which is a C1 control. Six glyphs shipped broken: the pagination
 * arrows, the back link, and the source/refresh ticks. Each rendered as a
 * three-character pile of debris in the browser, in the controls a user clicks
 * to move through a dataset.
 *
 * The class of bug is silent. Nothing fails to compile, no test fails, the
 * strings are valid JavaScript, and the UI simply shows garbage. So it is
 * checked here instead.
 *
 * The backend is deliberately excluded. `services/normalizer.py` and
 * `test_regressions_sep26.py` hold real mojibake on purpose, as fixtures for
 * `repair_mojibake`; flagging them would be flagging the tests that prove this
 * class of bug is handled on the data side.
 *
 * This file names its targets with escapes and never with the literal
 * characters, so that it does not trip its own detector.
 */

const ROOTS = ['src', 'e2e', 'test'];
const EXTENSIONS = new Set(['.js', '.jsx', '.mjs', '.css', '.html', '.json']);

function walk(dir, out = []) {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    const st = statSync(full);
    if (st.isDirectory()) {
      if (entry === 'node_modules' || entry === 'dist') continue;
      walk(full, out);
    } else if (EXTENSIONS.has(extname(entry))) {
      out.push(full);
    }
  }
  return out;
}

/**
 * Bytes that can only appear as the debris of a mis-decoded multi-byte
 * character. The cp1252 and cp1250 renderings of UTF-8 lead bytes (U+00C2 ..
 * U+00CA), and U+00E2, which is what a left arrow, a tick and a cross turn
 * into when they pass through a single-byte codepage.
 *
 * U+00B7 (middle dot) and U+00D7 (multiplication sign) are deliberately *not*
 * listed. They are ordinary characters this UI uses constantly as separators,
 * and an earlier detector flagged all 38 of them as corruption.
 */
const LEAD = /[\u00C2-\u00CA\u00E2]/;

/** The replacement character, which is what a failed decode leaves behind. */
const REPLACEMENT = '\uFFFD';

const files = ROOTS.filter((d) => {
  try {
    return statSync(d).isDirectory();
  } catch {
    return false;
  }
}).flatMap((d) => walk(d));

let checked = 0;
const failures = [];

for (const file of files) {
  const raw = readFileSync(file);
  const text = raw.toString('utf8');

  // A byte sequence that is not valid UTF-8 becomes U+FFFD when decoded. That
  // replacement character is the decoding failure made visible, and it is the
  // one signal that catches damage no lead-byte pattern would match.
  if (text.includes(REPLACEMENT)) {
    const line = text.slice(0, text.indexOf(REPLACEMENT)).split('\n').length;
    failures.push(`${relative('.', file)}:${line}  contains U+FFFD, a decoding failure`);
    continue;
  }

  if (LEAD.test(text)) {
    text.split('\n').forEach((line, i) => {
      if (!LEAD.test(line)) return;
      const m = line.match(LEAD);
      const at = line.indexOf(m[0]);
      const codes = [...line.slice(Math.max(0, at - 2), at + 6)]
        .map((c) => `U+${c.codePointAt(0).toString(16).toUpperCase().padStart(4, '0')}`)
        .join(' ');
      failures.push(
        `${relative('.', file)}:${i + 1}  possible mojibake near "${codes}" — ` +
          `  ${line.trim().slice(0, 70)}`
      );
    });
  }

  // Re-encode and compare. This catches a file that is valid UTF-8 but has a
  // BOM or a lone surrogate, both of which break a build differently.
  if (Buffer.from(text, 'utf8').compare(raw) !== 0) {
    failures.push(`${relative('.', file)}  does not round-trip as UTF-8`);
  }
  checked += 1;
}

console.log('=== source encoding ===');
if (failures.length) {
  console.log(`  FAIL  ${failures.length} problem(s) in ${checked} files`);
  for (const f of failures) console.log(`    ${f}`);
  console.log('');
  process.exit(1);
}
console.log(`  ok  ${checked} files are clean UTF-8, no mis-decoded glyphs`);
console.log('');
