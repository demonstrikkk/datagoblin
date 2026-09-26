/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors: {
        ink: 'var(--ink)',
        paper: 'var(--paper)',
        warm: 'var(--warm)',
        muted: 'var(--muted)',
        rule: 'var(--rule)',
        accent: 'var(--accent)',
      },
      fontFamily: {
        display: ['"Instrument Serif"', 'Newsreader', 'Georgia', 'serif'],
        sans: ['"DM Sans"', 'system-ui', 'sans-serif'],
      },
    },
  },
  plugins: [],
};
