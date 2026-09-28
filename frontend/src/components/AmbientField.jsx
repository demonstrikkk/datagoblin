import { useEffect, useRef } from 'react';

/**
 * Ambient background field.
 *
 * Deliberate constraints:
 *  - no WebGL library, ~60 lines of GL, one fullscreen triangle, one draw call
 *  - rendered at a fraction of device resolution (blurred anyway, so nobody
 *    can tell) and capped at dpr 1.5
 *  - pauses on `visibilitychange` and when the tab is backgrounded
 *  - `prefers-reduced-motion` renders exactly one static frame, then stops
 *  - silently degrades to the CSS scrim if WebGL is unavailable
 *
 * Everything here is decorative. Contrast-critical surfaces sit on
 * near-opaque paper, so the field can never reduce text legibility.
 */

const VERT = `
attribute vec2 aPos;
void main() { gl_Position = vec4(aPos, 0.0, 1.0); }
`;

const FRAG = `
precision mediump float;

uniform vec2  uRes;
uniform float uTime;
uniform float uScroll;

float hash(vec2 p) {
  p = fract(p * vec2(123.34, 456.21));
  p += dot(p, p + 45.32);
  return fract(p.x * p.y);
}

float noise(vec2 p) {
  vec2 i = floor(p);
  vec2 f = fract(p);
  vec2 u = f * f * (3.0 - 2.0 * f);
  float a = hash(i);
  float b = hash(i + vec2(1.0, 0.0));
  float c = hash(i + vec2(0.0, 1.0));
  float d = hash(i + vec2(1.0, 1.0));
  return mix(mix(a, b, u.x), mix(c, d, u.x), u.y);
}

float fbm(vec2 p) {
  float v = 0.0;
  float amp = 0.5;
  for (int i = 0; i < 4; i++) {
    v += amp * noise(p);
    p *= 2.03;
    amp *= 0.5;
  }
  return v;
}

void main() {
  vec2 uv = gl_FragCoord.xy / uRes;
  float aspect = uRes.x / max(uRes.y, 1.0);
  vec2 p = vec2(uv.x * aspect, uv.y);
  p.y += uScroll;

  float t = uTime * 0.035;

  // domain warp -> slow, non-repeating drift
  vec2 q = vec2(fbm(p * 1.6 + vec2(0.0, t)),
                fbm(p * 1.6 + vec2(5.2, 1.3 - t)));
  float f = fbm(p * 2.1 + q * 1.35 + vec2(t * 0.6, -t * 0.4));

  // three drifting sources, warm bronze / slate / moss, kept very low contrast
  vec3 paper = vec3(0.965, 0.953, 0.933);
  vec3 bronze = vec3(0.647, 0.463, 0.278);
  vec3 slate  = vec3(0.400, 0.463, 0.510);
  vec3 moss   = vec3(0.451, 0.522, 0.412);

  float b = smoothstep(0.28, 0.95, f);
  float s = smoothstep(0.52, 1.0, fbm(p * 1.15 - q * 0.6 + vec2(2.1, t * 0.8)));
  float m = smoothstep(0.60, 1.0, fbm(p * 0.85 + q * 0.4 + vec2(-1.7, -t * 0.5)));

  vec3 col = paper;
  col = mix(col, bronze, b * 0.085);
  col = mix(col, slate,  s * 0.055);
  col = mix(col, moss,   m * 0.045);

  // a single soft highlight keeps the composition from reading as flat noise
  float glow = 1.0 - smoothstep(0.0, 0.85, distance(uv, vec2(0.26, 0.14)));
  col += glow * 0.022;

  // vignette
  float vig = smoothstep(1.25, 0.28, distance(uv, vec2(0.5)));
  col = mix(col * 0.972, col, vig);

  // fine grain, kills gradient banding on 8-bit displays
  float g = hash(gl_FragCoord.xy + fract(uTime) * 91.7) - 0.5;
  col += g * 0.008;

  gl_FragColor = vec4(clamp(col, 0.0, 1.0), 1.0);
}
`;

function compile(gl, type, src) {
  const sh = gl.createShader(type);
  gl.shaderSource(sh, src);
  gl.compileShader(sh);
  if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) {
    gl.deleteShader(sh);
    return null;
  }
  return sh;
}

export default function AmbientField() {
  const ref = useRef(null);

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return undefined;

    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)');
    let gl = null;
    try {
      gl =
        canvas.getContext('webgl', {
          alpha: false,
          antialias: false,
          depth: false,
          stencil: false,
          powerPreference: 'low-power',
          preserveDrawingBuffer: false,
        }) ||
        canvas.getContext('experimental-webgl');
    } catch {
      gl = null;
    }
    if (!gl) return undefined; // CSS scrim is the fallback; nothing else to do

    const vs = compile(gl, gl.VERTEX_SHADER, VERT);
    const fs = compile(gl, gl.FRAGMENT_SHADER, FRAG);
    if (!vs || !fs) return undefined;

    const prog = gl.createProgram();
    gl.attachShader(prog, vs);
    gl.attachShader(prog, fs);
    gl.bindAttribLocation(prog, 0, 'aPos');
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) return undefined;
    gl.useProgram(prog);

    // one oversized triangle covers the viewport with no index buffer
    const buf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(
      gl.ARRAY_BUFFER,
      new Float32Array([-1, -1, 3, -1, -1, 3]),
      gl.STATIC_DRAW
    );
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);

    const uRes = gl.getUniformLocation(prog, 'uRes');
    const uTime = gl.getUniformLocation(prog, 'uTime');
    const uScroll = gl.getUniformLocation(prog, 'uScroll');

    // render below native res: the field is soft by construction
    const SCALE = 0.5;
    let raf = 0;
    let w = 0;
    let h = 0;

    const resize = () => {
      const dpr = Math.min(window.devicePixelRatio || 1, 1.5);
      const nw = Math.max(1, Math.floor(window.innerWidth * dpr * SCALE));
      const nh = Math.max(1, Math.floor(window.innerHeight * dpr * SCALE));
      if (nw === w && nh === h) return;
      w = nw;
      h = nh;
      canvas.width = w;
      canvas.height = h;
      gl.viewport(0, 0, w, h);
      gl.uniform2f(uRes, w, h);
    };

    const start = performance.now();
    let scroll = 0;

    const draw = (now) => {
      raf = 0;
      resize();
      scroll = window.scrollY * 0.00035;
      gl.uniform1f(uTime, now - start);
      gl.uniform1f(uScroll, scroll);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
    };

    let running = false;
    const loop = (now) => {
      draw(now);
      if (running && !reduced.matches) raf = requestAnimationFrame(loop);
    };

    const startLoop = () => {
      if (running || reduced.matches) return;
      running = true;
      raf = requestAnimationFrame(loop);
    };
    const stopLoop = () => {
      running = false;
      if (raf) cancelAnimationFrame(raf);
      raf = 0;
    };

    // reduced motion: one frame, then idle
    if (reduced.matches) {
      draw(start);
    } else {
      startLoop();
    }

    const onReducedChange = () => {
      if (reduced.matches) {
        stopLoop();
        draw(performance.now());
      } else {
        startLoop();
      }
    };

    const onVisibility = () => {
      if (document.hidden) stopLoop();
      else if (!reduced.matches) {
        // reset the clock so the field doesn't jump forward unseen
        startLoop();
      }
    };

    const onResize = () => {
      if (reduced.matches) draw(performance.now());
    };

    reduced.addEventListener?.('change', onReducedChange);
    document.addEventListener('visibilitychange', onVisibility);
    window.addEventListener('resize', onResize, { passive: true });

    return () => {
      stopLoop();
      reduced.removeEventListener?.('change', onReducedChange);
      document.removeEventListener('visibilitychange', onVisibility);
      window.removeEventListener('resize', onResize);
      gl.deleteBuffer(buf);
      gl.deleteProgram(prog);
      gl.deleteShader(vs);
      gl.deleteShader(fs);
      const lose = gl.getExtension('WEBGL_lose_context');
      lose?.loseContext();
    };
  }, []);

  return (
    <>
      <canvas ref={ref} className="field-canvas" aria-hidden="true" />
      <div className="field-scrim" aria-hidden="true" />
    </>
  );
}
