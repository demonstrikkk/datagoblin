/**
 * Chart decisions, checked against the real module.
 *
 * These two functions choose what a chart claims about a dataset. A wrong
 * choice does not crash — it draws a confident, meaningless picture, which is
 * the one failure mode this project cares most about. So they live in
 * lib/format.js (testable without a browser) and are exercised here directly.
 *
 * Run with:  npm run test:charts
 */
import assert from 'node:assert/strict';

import { numericShare, topCategories } from '../src/lib/format.js';

let failures = 0;

function check(name, fn) {
  try {
    fn();
    console.log(`  ok    ${name}`);
  } catch (e) {
    failures += 1;
    console.log(`  FAIL  ${name}\n        ${e.message.split('\n')[0]}`);
  }
}

console.log('=== numericShare ===');

check('a column of numbers is numeric', () => {
  const rows = [{ v: '1' }, { v: '2' }, { v: 3 }, { v: '4' }];
  assert.equal(numericShare(rows, 'v'), 1);
});

check('currency and separators still count as numbers', () => {
  const rows = [{ v: '$1,200' }, { v: '3.5%' }, { v: '1 000' }];
  assert.equal(numericShare(rows, 'v'), 1);
});

check('a column of words is not numeric', () => {
  assert.equal(numericShare([{ v: 'saas' }, { v: 'fintech' }], 'v'), 0);
});

check('a mostly-text column is not numeric', () => {
  // 0.5 is the boundary; a column of ids and labels must never become a
  // magnitude chart just because a few values happen to be digits.
  const rows = [{ v: 'a1' }, { v: 'b2' }, { v: '3' }, { v: 'c4' }];
  assert.ok(numericShare(rows, 'v') < 0.6);
});

check('empty and missing values are ignored, not counted as failures', () => {
  const rows = [{ v: '1' }, { v: '' }, { v: null }, { v: undefined }, { v: '2' }];
  assert.equal(numericShare(rows, 'v'), 1);
});

check('a missing column is 0, not a crash', () => {
  assert.equal(numericShare([{ v: 1 }], 'nope'), 0);
  assert.equal(numericShare([], 'v'), 0);
  assert.equal(numericShare(null, 'v'), 0);
});

check('non-finite text is not a number', () => {
  assert.equal(numericShare([{ v: 'NaN' }, { v: 'Infinity' }], 'v'), 0);
});

console.log('=== topCategories ===');

check('counts occurrences, biggest first', () => {
  const rows = [
    { v: 'a' }, { v: 'b' }, { v: 'a' }, { v: 'c' }, { v: 'a' }, { v: 'b' },
  ];
  assert.deepEqual(topCategories(rows, 'v'), [['a', 3], ['b', 2], ['c', 1]]);
});

check('a single distinct value is not a distribution', () => {
  // One full-width bar is not a chart; it is a sentence with axes.
  assert.deepEqual(topCategories([{ v: 'x' }, { v: 'x' }], 'v'), []);
});

check('blanks and nulls are not a category of their own', () => {
  // Two real categories plus junk: the junk must not appear as `""` with a
  // count of 2, which would be the single most misleading bar on the chart.
  const rows = [{ v: 'a' }, { v: '' }, { v: null }, { v: 'a' }, { v: 'b' }];
  assert.deepEqual(topCategories(rows, 'v'), [['a', 2], ['b', 1]]);
  assert.equal(topCategories(rows, 'v').some(([label]) => label === ''), false);
});

check('the limit is honoured and keeps the biggest', () => {
  const rows = ['a', 'b', 'c', 'd'].flatMap((k, i) =>
    Array.from({ length: i + 1 }, () => ({ v: k })));
  const out = topCategories(rows, 'v', 2);
  assert.equal(out.length, 2);
  assert.deepEqual(out.map((e) => e[0]), ['d', 'c']);
});

check('values are compared as strings, so 10 and 9 do not merge', () => {
  const rows = [{ v: 10 }, { v: 9 }];
  assert.equal(topCategories(rows, 'v').length, 2);
});

check('missing column and empty input are empty, not a crash', () => {
  assert.deepEqual(topCategories([{ v: 'a' }], 'nope'), []);
  assert.deepEqual(topCategories([], 'v'), []);
  assert.deepEqual(topCategories(null, 'v'), []);
});

console.log('='.repeat(52));
if (failures) {
  console.log(`${failures} FAILED`);
  process.exit(1);
}
console.log('ALL PASSED');
