/**
 * Chapter list for `datagoblin-explainer.mp4`.
 *
 * These are not authored timings. They are the scene boundaries ffmpeg detected
 * in the source recording:
 *
 *   ffmpeg -i DataGoblin_Extraction.mp4 \
 *     -filter:v "select='gt(scene,0.2)',metadata=print:file=-" -an -f null -
 *
 * Nineteen scene changes across 640.6s give twenty chapters. Each title below
 * was read off that chapter's own opening frame, so the navigation cannot drift
 * away from the video the way a hand-typed chapter list would.
 *
 * The cut served on the page is re-encoded without an audio track; the source
 * recording is 1280x720 at 24fps with sound.
 */

export const SOURCE = {
  duration: 640.6,
  width: 1280,
  height: 720,
  fps: 24,
  audioOnSource: true,
  audioInWebCut: false,
  encodedBy: 'ffmpeg · libx264 crf28 · faststart',
  authoredIn: 'Gemini Notebook',
  bytes: 5474457,
};

export const CHAPTERS = [
  { t: 0, label: 'DataGoblin Extraction' },
  { t: 26.583, label: 'The hallucination problem' },
  { t: 52.375, label: 'The rule' },
  { t: 81.25, label: 'Meet DataGoblin' },
  { t: 103.292, label: 'Garbage in' },
  { t: 131.958, label: 'Is / is not' },
  { t: 164.833, label: 'The five stages' },
  { t: 189.083, label: 'Never fabricate' },
  { t: 270.292, label: 'Offset verification' },
  { t: 297.625, label: 'Every field must fit' },
  { t: 325.583, label: 'Verified fields' },
  { t: 351.875, label: 'Trust and audit' },
  { t: 378.625, label: 'Market applications' },
  { t: 405.917, label: 'Find anything' },
  { t: 434.292, label: 'The open problem' },
  { t: 461.917, label: 'Built to hold' },
  { t: 482.458, label: 'Scale' },
  { t: 565.875, label: 'No AI garbage' },
  { t: 591.417, label: 'The rule, restated' },
  { t: 614.417, label: 'Can your pipeline say this?' },
];

export const RATES = [0.75, 1, 1.5, 2];

/** mm:ss, or h:mm:ss past an hour. */
export function clock(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return '0:00';
  const s = Math.floor(seconds % 60);
  const m = Math.floor((seconds / 60) % 60);
  const h = Math.floor(seconds / 3600);
  const mm = h > 0 ? String(m).padStart(2, '0') : String(m);
  return `${h > 0 ? `${h}:` : ''}${mm}:${String(s).padStart(2, '0')}`;
}

/** The chapter containing `t`, or the last one before the end. */
export function chapterAt(t) {
  let found = CHAPTERS[0];
  for (const c of CHAPTERS) {
    if (c.t <= t + 0.001) found = c;
    else break;
  }
  return found;
}