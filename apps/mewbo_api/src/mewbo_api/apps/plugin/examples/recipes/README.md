# Pipeline recipes

Each recipe pairs a compact `submit_app` pipeline declaration with its real source files. Copy the
nearest shape, then change the source and contract together. Every recipe declares a `result` and
`samples`; these make output and representative inputs visible to submit-time verification.

| Need | Recipe |
|---|---|
| A CLI returning JSON | `cli-json/` |
| A CLI returning fixed text | `cli-text/` |
| Workspace files materialized into a collection | `files-to-collection/` |
| A bounded schema-shaped model transform | `llm-transform/` |
| A frontend form that writes | `user-input/` |
| A semantic condition beyond a result schema | `verifier/` |

Errors are signal, never empty success. Raise where an invariant becomes known; narrow a tolerated
`ctx` failure by its `code`, and re-raise every other failure. The pipeline linter rejects broad
catches that hide guarded `ctx` errors.
