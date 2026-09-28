/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors: {
        // Declared as channel triplets so opacity modifiers resolve:
        // `bg-paper-2/90` only compiles against `rgb(var(--x) / <alpha-value>)`.
        ink: 'rgb(var(--ink) / <alpha-value>)',
        'ink-2': 'rgb(var(--ink-2) / <alpha-value>)',
        paper: 'rgb(var(--paper) / <alpha-value>)',
        'paper-2': 'rgb(var(--paper-2) / <alpha-value>)',
        'paper-3': 'rgb(var(--paper-3) / <alpha-value>)',
        warm: 'rgb(var(--warm) / <alpha-value>)',
        muted: 'rgb(var(--muted) / <alpha-value>)',
        rule: 'rgb(var(--rule) / <alpha-value>)',
        'rule-2': 'rgb(var(--rule-2) / <alpha-value>)',
        accent: 'rgb(var(--accent) / <alpha-value>)',
        'accent-ink': 'rgb(var(--accent-ink) / <alpha-value>)',
        ok: 'rgb(var(--ok) / <alpha-value>)',
        warn: 'rgb(var(--warn) / <alpha-value>)',
        danger: 'rgb(var(--danger) / <alpha-value>)',
        info: 'rgb(var(--info) / <alpha-value>)',
        judge: 'rgb(var(--judge) / <alpha-value>)',
        // Tinted fills for status surfaces. Kept as real tokens rather than
        // arbitrary `bg-[var(--x-soft)]` values so they can be @applied and
        // stay in sync with the base colour.
        'ok-soft': 'rgb(var(--ok) / 0.10)',
        'warn-soft': 'rgb(var(--warn) / 0.12)',
        'danger-soft': 'rgb(var(--danger) / 0.10)',
        'info-soft': 'rgb(var(--info) / 0.10)',
        'judge-soft': 'rgb(var(--judge) / 0.10)',
      },
      fontFamily: {
        display: ['"Instrument Serif"', 'Newsreader', 'Georgia', 'serif'],
        sans: ['"DM Sans"', 'system-ui', 'sans-serif'],
        mono: ['"DM Mono"', 'ui-monospace', 'SFMono-Regular', 'Menlo', 'monospace'],
      },
      boxShadow: {
        hair: '0 0 0 1px rgb(var(--rule))',
        lift: '0 1px 2px rgba(28,25,23,.04), 0 8px 24px -12px rgba(28,25,23,.18)',
        deep: '0 2px 4px rgba(28,25,23,.05), 0 24px 60px -24px rgba(28,25,23,.28)',
        glow: '0 0 0 1px rgb(var(--accent)), 0 0 0 4px var(--accent-ring)',
      },
      borderRadius: {
        xs: '3px',
        sm: '5px',
        DEFAULT: '7px',
        md: '9px',
        lg: '13px',
        xl: '18px',
      },
      transitionTimingFunction: {
        swift: 'cubic-bezier(.22,.61,.36,1)',
        spring: 'cubic-bezier(.34,1.42,.5,1)',
        exit: 'cubic-bezier(.4,0,1,1)',
      },
      keyframes: {
        'fade-up': {
          from: { opacity: '0', transform: 'translate3d(0,10px,0)' },
          to: { opacity: '1', transform: 'none' },
        },
        'fade-in': { from: { opacity: '0' }, to: { opacity: '1' } },
        'scale-in': {
          from: { opacity: '0', transform: 'scale(.96) translate3d(0,4px,0)' },
          to: { opacity: '1', transform: 'none' },
        },
        'slide-left': {
          from: { opacity: '0', transform: 'translate3d(14px,0,0)' },
          to: { opacity: '1', transform: 'none' },
        },
        shimmer: {
          '0%': { backgroundPosition: '-200% 0' },
          '100%': { backgroundPosition: '200% 0' },
        },
        'pulse-ring': {
          '0%': { transform: 'scale(.85)', opacity: '.7' },
          '70%': { transform: 'scale(2.1)', opacity: '0' },
          '100%': { transform: 'scale(2.1)', opacity: '0' },
        },
        'dash-flow': { to: { strokeDashoffset: '-24' } },
        'rise-in': {
          from: { opacity: '0', transform: 'translate3d(0,4px,0) scale(.99)' },
          to: { opacity: '1', transform: 'none' },
        },
      },
      animation: {
        'fade-up': 'fade-up .5s cubic-bezier(.22,.61,.36,1) both',
        'fade-in': 'fade-in .35s ease both',
        'scale-in': 'scale-in .22s cubic-bezier(.34,1.42,.5,1) both',
        'slide-left': 'slide-left .26s cubic-bezier(.22,.61,.36,1) both',
        shimmer: 'shimmer 1.9s linear infinite',
        'pulse-ring': 'pulse-ring 1.8s cubic-bezier(.22,.61,.36,1) infinite',
        'dash-flow': 'dash-flow .7s linear infinite',
        'rise-in': 'rise-in .3s cubic-bezier(.22,.61,.36,1) both',
      },
    },
  },
  plugins: [],
};
