# data/

Local runtime state. Not source code.

When Mewbo runs with the JSON storage driver, session records land in
`data/sessions/` by default (`runtime.session_dir`, see
`packages/mewbo_core/src/mewbo_core/session_store.py`). Switching to the
MongoDB driver bypasses this directory entirely.

Everything here except this file and `.gitignore` is ignored by git. Safe to
delete when you want a clean slate; Mewbo recreates it on the next run.
