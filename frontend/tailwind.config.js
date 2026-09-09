/** @type {import("tailwindcss").Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: { 
    extend: {
      colors: {
        background: '#F4F5F7',
        foreground: '#111111',
        primary: {
          DEFAULT: '#E63946',
          foreground: '#FFFFFF',
        },
        accent: {
          DEFAULT: '#4AC1AA',
          foreground: '#FFFFFF',
        },
        card: {
          DEFAULT: '#FFFFFF',
          foreground: '#111111',
        },
        muted: '#64748B'
      },
      fontFamily: {
        sans: ['Inter', 'sans-serif'],
        mono: ['IBM Plex Mono', 'monospace'],
        display: ['Unbounded', 'sans-serif'],
      },
      boxShadow: {
        'glass': '0 4px 30px rgba(0, 0, 0, 0.05)',
        'solid': '4px 4px 0px rgba(17, 17, 17, 1)',
      }
    } 
  },
  plugins: [],
}
