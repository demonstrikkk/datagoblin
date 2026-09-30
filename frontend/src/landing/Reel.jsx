/**
 * The explainer, presented as a recorded exhibit rather than an embedded player.
 *
 * A native `<video controls>` in a rounded box would be the category default and
 * would also throw away the page's own vocabulary. Instead this is a dossier
 * object with a transport built from the same primitives as the proof rail: a
 * mono timecode, a real scrubbable rail, chapter ticks resolved from the
 * recording's own scene changes, and a pointer readout in the frame's
 * coordinates — which is the same idea as the citation offsets, applied to a
 * different surface.
 *
 * Cost discipline: nothing is fetched until a reader asks for it. `preload` is
 * `none` and the source is attached on the first play, so a visitor who never
 * presses play never downloads the 5.4MB cut.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { CHAPTERS, RATES, SOURCE, chapterAt, clock } from './chapters.js';

const SRC = '/media/datagoblin-explainer.mp4';
const POSTER = '/media/explainer-poster.jpg';

function usePrefersReducedMotion() {
  const [reduced, setReduced] = useState(false);
  useEffect(() => {
    const mq = matchMedia('(prefers-reduced-motion: reduce)');
    const read = () => setReduced(mq.matches);
    read();
    mq.addEventListener?.('change', read);
    return () => mq.removeEventListener?.('change', read);
  }, []);
  return reduced;
}

/* ==========================================================================
   Transport
   ========================================================================== */

function Reel() {
  const reduced = usePrefersReducedMotion();
  const videoRef = useRef(null);
  const railRef = useRef(null);

  const [attached, setAttached] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [ended, setEnded] = useState(false);
  const [time, setTime] = useState(0);
  const [dur, setDur] = useState(SOURCE.duration);
  const [rate, setRate] = useState(1);
  const [zoom, setZoom] = useState(null); // {x,y} in %
  const [inspecting, setInspecting] = useState(false);

  const chapter = useMemo(() => chapterAt(time), [time]);
  const progress = dur > 0 ? Math.min(1, time / dur) : 0;

  /* Attach the source on first intent. This is the whole reason the file can
     afford to be a real recording. */
  const attach = useCallback(() => setAttached(true), []);

  const play = useCallback(() => {
    attach();
    const v = videoRef.current;
    if (!v) return;
    const p = v.play();
    if (p?.catch) p.catch(() => setPlaying(false));
  }, [attach]);

  const toggle = useCallback(() => {
    const v = videoRef.current;
    if (!v) return;
    if (v.paused || v.ended) play();
    else v.pause();
  }, [play]);

  const seek = useCallback(
    (next) => {
      const v = videoRef.current;
      if (!v) {
        setTime(next);
        return;
      }
      v.currentTime = next;
      setTime(next);
    },
    [],
  );

  const jump = useCallback(
    (t) => {
      seek(t + 0.05);
      railRef.current?.focus?.();
    },
    [seek],
  );

  const cycleRate = useCallback(() => {
    setRate((r) => {
      const next = RATES[(RATES.indexOf(r) + 1) % RATES.length];
      const v = videoRef.current;
      if (v) v.playbackRate = next;
      return next;
    });
  }, []);

  /* Media element events are the clock here — no rAF loop, nothing running while
     the reader is not watching. */
  const onMeta = useCallback(() => {
    const v = videoRef.current;
    if (v && Number.isFinite(v.duration) && v.duration > 0) setDur(v.duration);
  }, []);

  const onTime = useCallback(() => {
    const v = videoRef.current;
    if (v) setTime(v.currentTime);
  }, []);

  const onEnded = useCallback(() => {
    setPlaying(false);
    setEnded(true);
  }, []);

  useEffect(() => {
    const v = videoRef.current;
    if (v) v.playbackRate = rate;
  }, [rate]);

  /* Space and arrows while the transport has focus: the shortcuts a reader
     expects from a player, without hijacking the page. */
  const onKeyDown = (e) => {
    const step = e.shiftKey ? 30 : 5;
    if (e.key === ' ' || e.key === 'k') {
      e.preventDefault();
      toggle();
    } else if (e.key === 'ArrowRight') {
      e.preventDefault();
      seek(time + step);
    } else if (e.key === 'ArrowLeft') {
      e.preventDefault();
      seek(time - step);
    } else if (e.key === 'Home') {
      e.preventDefault();
      seek(0);
    } else if (e.key === 'Enter') {
      e.preventDefault();
      setInspecting(true);
    }
  };

  const onPointerMove = (e) => {
    if (reduced) return;
    const r = e.currentTarget.getBoundingClientRect();
    setZoom({
      x: ((e.clientX - r.left) / r.width) * 100,
      y: ((e.clientY - r.top) / r.height) * 100,
    });
  };

  return (
    <div className="lg-reel">
      <div className="lg-reel-head">
        <span className="lg-reel-badge" aria-hidden="true" />
        <span className="lg-tag">
          Recorded explainer · {SOURCE.width}×{SOURCE.height} · {clock(SOURCE.duration)}
        </span>
        <span className="lg-tag lg-reel-src">{SOURCE.authoredIn}</span>
      </div>

      <div
        className="lg-reel-stage"
        data-started={attached ? 'true' : 'false'}
        onPointerMove={onPointerMove}
        onPointerLeave={() => setZoom(null)}
        onClick={(e) => {
          // The stage is a button before it is a player; after that a click is
          // play/pause, and the inspect affordance is its own control.
          if (!attached) play();
          else if (e.target.closest('[data-nozoom]')) return;
          else toggle();
        }}
        onKeyDown={onKeyDown}
        tabIndex={0}
        role={attached ? 'group' : 'button'}
        aria-label={
          attached
            ? 'Recorded explainer. Space plays, arrows seek, Enter inspects.'
            : `Play the recorded explainer, ${clock(SOURCE.duration)} long`
        }
      >
        <video
          ref={videoRef}
          className="lg-reel-video"
          poster={POSTER}
          preload="none"
          playsInline
          muted
          onClick={(e) => e.stopPropagation()}
          onLoadedMetadata={onMeta}
          onTimeUpdate={onTime}
          onPlay={() => {
            setPlaying(true);
            setEnded(false);
          }}
          onPause={() => setPlaying(false)}
          onEnded={onEnded}
        >
          {attached ? <source src={SRC} type="video/mp4" /> : null}
        </video>

        {/* The poster drifts until the reader plays. It is the one piece of
            ambient motion in the section, and it stops dead on interaction. */}
        {!attached ? (
          <div
            className="lg-reel-poster"
            aria-hidden="true"
            data-drift={reduced ? 'off' : 'on'}
          />
        ) : null}

        {/* The play ring: an expanding circle on a loop, so the target reads as
            live without a video thumbnail pretending to be a click target. */}
        {!playing ? (
          <span className="lg-reel-play" aria-hidden="true" data-pulse={reduced ? 'off' : 'on'}>
            <svg viewBox="0 0 24 24" width="26" height="26" focusable="false">
              <path d="M8 5.5v13l11-6.5z" fill="currentColor" />
            </svg>
          </span>
        ) : null}

        {ended ? (
          <span className="lg-reel-replay">
            <span>Replay</span>
          </span>
        ) : null}

        {/* A pointer readout in frame coordinates. The page is about citing
            character positions, so the player reports its own. */}
        {zoom && playing ? (
          <span className="lg-reel-readout lg-mono" aria-hidden="true">
            x {zoom.x.toFixed(3)} · y {zoom.y.toFixed(3)}
          </span>
        ) : null}
      </div>

      {/* --- transport ------------------------------------------------------ */}
      <div className="lg-reel-bar" data-nozoom>
        <button
          type="button"
          className="lg-reel-btn"
          onClick={toggle}
          aria-label={playing ? 'Pause' : 'Play'}
          aria-pressed={playing}
        >
          {playing ? (
            <svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true">
              <rect x="7" y="5" width="3.4" height="14" fill="currentColor" />
              <rect x="13.6" y="5" width="3.4" height="14" fill="currentColor" />
            </svg>
          ) : (
            <svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true">
              <path d="M8 5.5v13l11-6.5z" fill="currentColor" />
            </svg>
          )}
        </button>

        <span className="lg-reel-time lg-mono">
          {clock(time)} <span className="lg-reel-time-sep">/</span> {clock(dur)}
        </span>

        <div className="lg-reel-rail-wrap">
          <input
            ref={railRef}
            className="lg-reel-rail"
            type="range"
            min={0}
            max={Math.max(1, dur)}
            step={0.05}
            value={time}
            aria-label="Seek"
            aria-valuetext={`${clock(time)} — ${chapter.label}`}
            onChange={(e) => seek(Number(e.target.value))}
            style={{ '--lg-p': `${progress * 100}%` }}
          />
          <span className="lg-reel-ticks" aria-hidden="true">
            {CHAPTERS.slice(1).map((c) => (
              <i
                key={c.t}
                data-past={c.t <= time ? 'true' : 'false'}
                style={{ left: `${(c.t / dur) * 100}%` }}
              />
            ))}
          </span>
        </div>

        <button
          type="button"
          className="lg-reel-btn lg-reel-rate lg-mono"
          onClick={cycleRate}
          aria-label={`Playback speed ${rate}×`}
        >
          {rate}×
        </button>

        <button
          type="button"
          className="lg-reel-btn"
          onClick={() => setInspecting(true)}
          aria-label="Inspect this frame full screen"
          data-nozoom
        >
          <svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true">
            <path
              d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.9"
              strokeLinecap="round"
            />
          </svg>
        </button>
      </div>

      <div className="lg-reel-now" data-nozoom>
        <span className="lg-tag">Now</span>
        <span className="lg-reel-chapter">{chapter.label}</span>
      </div>

      {/* --- chapters ------------------------------------------------------- */}
      <div className="lg-reel-chapters" data-nozoom>
        <span className="lg-tag lg-reel-chapters-label">
          Chapters · detected from the recording
        </span>
        <ul>
          {CHAPTERS.map((c, i) => (
            <li key={c.t}>
              <button
                type="button"
                data-active={chapter.label === c.label ? 'true' : 'false'}
                onClick={() => jump(c.t)}
              >
                <span className="lg-mono lg-reel-chapter-t">{clock(c.t)}</span>
                <span className="lg-reel-chapter-n">{c.label}</span>
                <span className="sr-only">
                  , chapter {i + 1} of {CHAPTERS.length}
                </span>
              </button>
            </li>
          ))}
        </ul>
      </div>

      {inspecting ? (
        <Inspector
          time={time}
          seek={seek}
          onClose={() => setInspecting(false)}
          reduced={reduced}
        />
      ) : null}
    </div>
  );
}

/* ==========================================================================
   Full-screen inspect. Zoom and pan on a recorded frame, because the page's
   whole argument is that a claim deserves a closer look.
   ========================================================================== */

function Inspector({ time, seek, onClose, reduced }) {
  const ref = useRef(null);
  const [scale, setScale] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const drag = useRef(null);

  useEffect(() => {
    ref.current?.focus();
  }, []);

  useEffect(() => {
    const onKey = (e) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const zoomBy = (d) =>
    setScale((s) => Math.min(6, Math.max(1, Number((s + d).toFixed(2)))));

  const clampPan = (next, s) => {
    const limit = 260 * (s - 1);
    return {
      x: Math.max(-limit, Math.min(limit, next.x)),
      y: Math.max(-limit, Math.min(limit, next.y)),
    };
  };

  return (
    <div
      className="lg-inspect"
      role="dialog"
      aria-modal="true"
      aria-label="Frame inspector"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="lg-inspect-panel" ref={ref} tabIndex={-1}>
        <div className="lg-inspect-head">
          <span className="lg-tag">Inspect · {clock(time)}</span>
          <div className="lg-inspect-tools">
            <button type="button" className="lg-reel-btn" onClick={() => zoomBy(-0.5)} aria-label="Zoom out">
              −
            </button>
            <span className="lg-mono lg-inspect-scale">{scale.toFixed(1)}×</span>
            <button type="button" className="lg-reel-btn" onClick={() => zoomBy(0.5)} aria-label="Zoom in">
              +
            </button>
            <button
              type="button"
              className="lg-reel-btn"
              onClick={() => {
                setScale(1);
                setPan({ x: 0, y: 0 });
              }}
            >
              Reset
            </button>
            <button type="button" className="lg-reel-btn" onClick={onClose} aria-label="Close inspector">
              Close
            </button>
          </div>
        </div>

        <div
          className="lg-inspect-view"
          data-grab={scale > 1 ? (reduced ? 'off' : 'on') : 'off'}
          onPointerDown={(e) => {
            if (scale <= 1 || reduced) return;
            drag.current = { x: e.clientX - pan.x, y: e.clientY - pan.y };
            e.currentTarget.setPointerCapture?.(e.pointerId);
          }}
          onPointerMove={(e) => {
            if (!drag.current) return;
            setPan((p) => clampPan({ x: e.clientX - drag.current.x, y: e.clientY - drag.current.y }, scale));
          }}
          onPointerUp={(e) => {
            drag.current = null;
            e.currentTarget.releasePointerCapture?.(e.pointerId);
          }}
          onWheel={(e) => {
            if (reduced) return;
            e.preventDefault();
            zoomBy(e.deltaY > 0 ? -0.25 : 0.25);
          }}
        >
          <video
            className="lg-inspect-video"
            src={SRC}
            poster={POSTER}
            preload="metadata"
            muted
            playsInline
            style={{ transform: `translate3d(${pan.x}px, ${pan.y}px, 0) scale(${scale})` }}
            onLoadedMetadata={(e) => {
              e.currentTarget.currentTime = time;
            }}
            onClick={() => {
              const v = document.querySelector('.lg-inspect-video');
              if (v?.paused) v.play().catch(() => {});
              else v?.pause();
            }}
            onTimeUpdate={(e) => seek(e.currentTarget.currentTime)}
          />
        </div>

        <p className="lg-fine lg-inspect-foot">
          Scroll to zoom, drag to pan, Escape to close. Playback here stays in step with the
          transport on the page.
        </p>
      </div>
    </div>
  );
}

export default Reel;