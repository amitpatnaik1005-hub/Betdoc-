/** @type {import("tailwindcss").Config} */
export default {
  darkMode: 'class',
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: ['Inter', 'ui-sans-serif', 'system-ui', 'sans-serif'],
        display: ['"Plus Jakarta Sans"', 'Inter', 'ui-sans-serif', 'sans-serif'],
        mono: ['"Geist Mono"', 'ui-monospace', 'SFMono-Regular', 'monospace'],
      },
      boxShadow: {
        // Warm, diffused lift. Never coloured, never glowing.
        soft: '0 1px 2px rgba(41, 37, 36, 0.04), 0 4px 16px -4px rgba(41, 37, 36, 0.06)',
        'soft-lg': '0 2px 4px rgba(41, 37, 36, 0.04), 0 12px 32px -8px rgba(41, 37, 36, 0.10)',
      },
      keyframes: {
        breathe: { '0%, 100%': { opacity: '1' }, '50%': { opacity: '0.45' } },
      },
      animation: {
        breathe: 'breathe 2.8s ease-in-out infinite',
      },
    },
  },
  plugins: [],
}
