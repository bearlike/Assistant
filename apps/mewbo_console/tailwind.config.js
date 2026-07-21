/* eslint-env node */
module.exports = {content: [
  './index.html',
  './src/**/*.{js,ts,jsx,tsx}'
],
  darkMode: "selector",
  theme: {
    container: {
      center: true,
      padding: "2rem",
      screens: {
        "2xl": "1400px",
      },
    },
    extend: {
      // Brand font stacks (mirror index.css body/code rules). Without these,
      // any `font-sans`/`font-mono` utility resolves to Tailwind's ui-sans-serif
      // defaults and silently overrides the brand fonts — the app-root layout
      // div carries `font-sans`, so the default stack would win over body's
      // Inter for the whole tree.
      // Two families, chosen for being battle-tested rather than novel. Inter
      // carries a 100-900 variable weight axis, which is the mechanism the whole
      // type system leans on: hierarchy here is expressed through WEIGHT and
      // colour, never through a second typeface or a 1px size step.
      //
      // `mono` is for machine text a reader might copy, diff, or align — code,
      // terminal output, diffs, paths, ids, cron expressions, column-aligned
      // numerals. It is not a texture for making a label look technical; that
      // usage spread to hundreds of call sites and is what made the interface
      // read as incoherent.
      fontFamily: {
        sans: ["'Inter Variable'", "system-ui", "-apple-system", "sans-serif"],
        mono: ["'JetBrains Mono Variable'", "ui-monospace", "monospace"],
      },
      // The console's ONE type scale. Every font size in the app resolves to a
      // step here; arbitrary `text-[13px]`-style values are rejected by
      // `typographyScale.test.ts`. Sizes are rem so they honour a user's root
      // font-size preference.
      //
      // `sm` and `base` are deliberately REDEFINED away from Tailwind's stock
      // 14/16px to 13/14px: the console is an instrument panel, and the whole
      // scale is one step denser than a content site's. Renaming them instead
      // would have meant rewriting ~570 already-correct call sites to say the
      // same thing.
      //
      // Usage law (enforced in review, not by the compiler): a single surface
      // uses at most THREE ADJACENT steps. Hierarchy inside a surface comes
      // from weight and colour — never from a 1px size step, which reads as
      // noise rather than structure.
      fontSize: {
        "2xs": ["0.6875rem", { lineHeight: "1.45" }], // 11px — metadata, chips, badges, timestamps
        xs: ["0.75rem", { lineHeight: "1.45" }], // 12px — secondary / supporting
        sm: ["0.8125rem", { lineHeight: "1.5" }], // 13px — dense body: rail rows, card bodies, list rows
        base: ["0.875rem", { lineHeight: "1.55" }], // 14px — primary body
        lg: ["1rem", { lineHeight: "1.5" }], // 16px — section titles
        xl: ["1.25rem", { lineHeight: "1.4" }], // 20px — pane titles
        "2xl": ["1.75rem", { lineHeight: "1.25" }], // 28px — landing headline (the largest type in the app)
        // `text-field` is NOT a scale step — it is a floor. iOS Safari zooms the
        // viewport whenever a focused text input computes below 16px, and `base`
        // is 14px here, so every <input>/<textarea> must carry this instead of
        // following body type down. Never use it for non-input text.
        //
        // Named `field`, NOT `input`: `colors.input` already exists (shadcn's
        // border token), and a key present in both `fontSize` and `colors`
        // generates the SAME utility twice. The colour rule is emitted later and
        // wins, so `text-input` would have set `color: hsl(var(--input))` — the
        // dim border colour — making typed text nearly invisible. `cn()` cannot
        // rescue it either: tailwind-merge classifies `text-input` as a colour
        // and would drop the foreground class instead of the size one.
        field: ["1rem", { lineHeight: "1.5" }],
      },
      colors: {
        border: "hsl(var(--border))",
        input: "hsl(var(--input))",
        ring: "hsl(var(--ring))",
        background: "hsl(var(--background))",
        foreground: "hsl(var(--foreground))",
        primary: {
          DEFAULT: "hsl(var(--primary))",
          foreground: "hsl(var(--primary-foreground))",
        },
        secondary: {
          DEFAULT: "hsl(var(--secondary))",
          foreground: "hsl(var(--secondary-foreground))",
        },
        destructive: {
          DEFAULT: "hsl(var(--destructive))",
          foreground: "hsl(var(--destructive-foreground))",
        },
        success: {
          DEFAULT: "hsl(var(--success))",
          foreground: "hsl(var(--success-foreground))",
        },
        warning: {
          DEFAULT: "hsl(var(--warning))",
          foreground: "hsl(var(--warning-foreground))",
        },
        info: {
          DEFAULT: "hsl(var(--info))",
          foreground: "hsl(var(--info-foreground))",
        },
        muted: {
          DEFAULT: "hsl(var(--muted))",
          foreground: "hsl(var(--muted-foreground))",
        },
        accent: {
          DEFAULT: "hsl(var(--accent))",
          foreground: "hsl(var(--accent-foreground))",
        },
        popover: {
          DEFAULT: "hsl(var(--popover))",
          foreground: "hsl(var(--popover-foreground))",
        },
        card: {
          DEFAULT: "hsl(var(--card))",
          foreground: "hsl(var(--card-foreground))",
        },
        permission: "hsl(var(--permission))",
        "plan-mode": "hsl(var(--plan-mode))",
        "user-msg": "hsl(var(--user-message-bg))",
        "user-msg-hover": "hsl(var(--user-message-bg-hover))",
        "bash-bg": "hsl(var(--bash-bg))",
        "memory-bg": "hsl(var(--memory-bg))",
        "agent-0": "hsl(var(--agent-0))",
        "agent-1": "hsl(var(--agent-1))",
        "agent-2": "hsl(var(--agent-2))",
        "agent-3": "hsl(var(--agent-3))",
        "agent-4": "hsl(var(--agent-4))",
        "agent-5": "hsl(var(--agent-5))",
        "agent-6": "hsl(var(--agent-6))",
        "agent-7": "hsl(var(--agent-7))",
      },
      borderRadius: {
        lg: "var(--radius)",
        md: "calc(var(--radius) - 2px)",
        sm: "calc(var(--radius) - 4px)",
      },
      keyframes: {
        "caret-blink": {
          "0%,70%,100%": { opacity: "1" },
          "20%,50%": { opacity: "0" },
        },
        "accordion-down": {
          from: { height: 0 },
          to: { height: "var(--radix-accordion-content-height)" },
        },
        "accordion-up": {
          from: { height: "var(--radix-accordion-content-height)" },
          to: { height: 0 },
        },
        "collapsible-down": {
          from: { height: 0 },
          to: { height: "var(--radix-collapsible-content-height)" },
        },
        "collapsible-up": {
          from: { height: "var(--radix-collapsible-content-height)" },
          to: { height: 0 },
        },
      },
      animation: {
        "accordion-down": "accordion-down 0.2s ease-out",
        "accordion-up": "accordion-up 0.2s ease-out",
        "collapsible-down": "collapsible-down 0.2s ease-out",
        "collapsible-up": "collapsible-up 0.2s ease-out",
        "caret-blink": "caret-blink 1.25s ease-out infinite",
      },
    },
  },
  plugins: [require("tailwindcss-animate")],
}
