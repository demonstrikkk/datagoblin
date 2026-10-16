/**
 * Client-side regressions that shipped as silently broken UI.
 *
 * Run directly: `node test/api.test.mjs`. No test framework — the rest of the
 * suite is plain asserts against `node:test`-free helpers, and adding a runner
 * for four files would be more dependency than test.
 *
 * Each case below is a defect that produced no error. The rejudge button threw
 * a ReferenceError only when clicked; the export wrote a file whose name
 * disagreed with its contents; the density toggle persisted a preference nothing
 * read. None of them would be caught by a build or a type check.
 */
import assert from 'node:assert/strict';
import test from 'node:test';

import { exportFilename, readEventStream } from '../src/lib/api.js';

test('export filename matches the format the server used', () => {
  // The envelope is `{data:{format, content}}` for every format; the filename
  // has to follow `format`, not whatever the caller assumed.
  assert.equal(exportFilename('87d1ef54-d70a-4a97', 'csv'), 'dataset-87d1ef54.csv');
  assert.equal(exportFilename('87d1ef54-d70a-4a97', 'md'), 'dataset-87d1ef54.md');
  assert.equal(exportFilename('87d1ef54-d70a-4a97', 'json'), 'dataset-87d1ef54.json');
  assert.equal(exportFilename('87d1ef54-d70a-4a97', 'jsonl'), 'dataset-87d1ef54.jsonl');
});

test('an unknown export format falls back rather than producing a nameless file', () => {
  assert.equal(exportFilename('abcdef12', 'xlsx'), 'dataset-abcdef12.txt');
  assert.equal(exportFilename('', 'csv'), 'dataset-export.csv');
});

/** Build a ReadableStream that emits the given chunks. */
function streamOf(chunks) {
  const encoder = new TextEncoder();
  let i = 0;
  return new ReadableStream({
    pull(controller) {
      if (i >= chunks.length) {
        controller.close();
        return;
      }
      controller.enqueue(encoder.encode(chunks[i]));
      i += 1;
    },
  });
}

test('SSE frames are parsed with their event name', async () => {
  const frames = [];
  await readEventStream(streamOf([
    'event: stage.started\ndata: {"stage":"DISCOVERING"}\n\n',
    'event: record.extracted\ndata: {"count":3}\n\n',
  ]), { onFrame: (f) => frames.push(f) });

  assert.equal(frames.length, 2);
  assert.equal(frames[0].event, 'stage.started');
  assert.equal(frames[0].data.stage, 'DISCOVERING');
  assert.equal(frames[1].event, 'record.extracted');
  assert.equal(frames[1].data.count, 3);
});

test('an unnamed frame arrives as `message`, as EventSource did', async () => {
  const frames = [];
  await readEventStream(streamOf(['data: {"type":"run.completed"}\n\n']),
    { onFrame: (f) => frames.push(f) });
  assert.equal(frames.length, 1);
  assert.equal(frames[0].event, 'message');
  assert.equal(frames[0].data.type, 'run.completed');
});

test('a frame split across chunks is not lost or double-counted', async () => {
  // This is why the parser buffers: TCP does not respect frame boundaries.
  const frames = [];
  await readEventStream(streamOf([
    'event: stage.pro',
    'gress\ndata: {"stag',
    'e":"FETCHING","n":1}',
    '\n\n',
  ]), { onFrame: (f) => frames.push(f) });
  assert.equal(frames.length, 1);
  assert.equal(frames[0].event, 'stage.progress');
  assert.equal(frames[0].data.n, 1);
});

test('several frames arriving in one chunk are all delivered', async () => {
  const frames = [];
  await readEventStream(streamOf([
    'event: a\ndata: 1\n\nevent: b\ndata: 2\n\nevent: c\ndata: 3\n\n',
  ]), { onFrame: (f) => frames.push(f) });
  assert.deepEqual(frames.map((f) => f.event), ['a', 'b', 'c']);
});

test('comments and keep-alives are ignored', async () => {
  const frames = [];
  await readEventStream(streamOf([
    ': keep-alive\n\n',
    'event: run.completed\ndata: {"status":"COMPLETED"}\n\n',
  ]), { onFrame: (f) => frames.push(f) });
  assert.equal(frames.length, 1, 'the keep-alive produced a phantom frame');
  assert.equal(frames[0].event, 'run.completed');
});

test('CRLF line endings are normalised', async () => {
  // A proxy rewriting line endings would otherwise leave a lone \r that never
  // matches the \n\n frame separator, so nothing ever parsed.
  const frames = [];
  await readEventStream(streamOf([
    'event: stage.started\r\ndata: {"stage":"EXTRACTING"}\r\n\r\n',
  ]), { onFrame: (f) => frames.push(f) });
  assert.equal(frames.length, 1);
  assert.equal(frames[0].event, 'stage.started');
});

test('non-JSON data is passed through as text rather than dropped', async () => {
  const frames = [];
  await readEventStream(streamOf(['event: log\ndata: not json\n\n']),
    { onFrame: (f) => frames.push(f) });
  assert.equal(frames[0].data, 'not json');
});

test('a multi-line data payload is joined, per the SSE spec', async () => {
  const frames = [];
  await readEventStream(streamOf(['event: x\ndata: {"a":\ndata: 1}\n\n']),
    { onFrame: (f) => frames.push(f) });
  assert.deepEqual(frames[0].data, { a: 1 });
});

test('an abort signal stops the reader', async () => {
  const controller = new AbortController();
  controller.abort();
  let called = false;
  await readEventStream(streamOf(['event: a\ndata: 1\n\n']), {
    signal: controller.signal,
    onFrame: () => { called = true; },
  });
  assert.equal(called, false, 'frames were delivered after abort');
});

test('an empty body is not an error', async () => {
  await readEventStream(null, { onFrame: () => assert.fail('should not fire') });
  await readEventStream(streamOf([]), { onFrame: () => assert.fail('should not fire') });
});