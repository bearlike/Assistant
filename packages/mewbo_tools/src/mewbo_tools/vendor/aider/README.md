# Vendored Aider Components

Source: https://github.com/Aider-AI/aider
Commit: 4bf56b77145b0be593ed48c3c90cdecead217496
License: Apache-2.0 (see `LICENSE.txt`)

## Files vendored
- `io.py`
- `mdstream.py`
- `editor.py`
- `dump.py`
- `run_cmd.py`
- `file_ops.py` (from `commands.py:expand_subdir`)
- `utils.py` (trimmed to `is_image_file` + `IMAGE_EXTENSIONS` only)

## Local modifications
- Updated absolute imports (`from aider...`) to local relative imports.
- Trimmed `utils.py` to avoid pulling Aider's full dependency tree.
