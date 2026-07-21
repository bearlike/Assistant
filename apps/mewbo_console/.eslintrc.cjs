module.exports = {
  root: true,
  env: { browser: true, es2020: true },
  extends: [
    'eslint:recommended',
    'plugin:@typescript-eslint/recommended',
    'plugin:react-hooks/recommended',
  ],
  // `public/` is Vite's verbatim-copy directory: nothing in it is authored by
  // us or processed by the bundler, and the only JS it ever contains is the
  // vendored Pyodide runtime that `scripts/fetch-pyodide.mjs` downloads (~26 MB,
  // gitignored). Linting it reported ~330 errors and ~320 warnings in a file we
  // do not own and cannot fix, which drowned the handful of real findings in
  // `src/` and made `--max-warnings=0` permanently unsatisfiable.
  ignorePatterns: ['dist', 'dist-widget-host', 'coverage', 'test-results', 'public', '.eslintrc.cjs'],
  parser: '@typescript-eslint/parser',
  plugins: ['react-refresh'],
  rules: {
    'react-refresh/only-export-components': [
      'warn',
      { allowConstantExport: true },
    ],
    '@typescript-eslint/no-unused-vars': [
      'warn',
      { argsIgnorePattern: '^_', varsIgnorePattern: '^_' },
    ],
  },
}
