import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    // happy-dom is fast and implements customElements + lifecycle callbacks,
    // which is what our light-DOM components need. Add `// @vitest-environment jsdom`
    // atop a file only if it needs fuller CSS-cascade fidelity.
    environment: "happy-dom",
    globals: true,
    include: ["orb/**/*.test.js"],
  },
});
