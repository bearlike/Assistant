# Getting Started

## Prerequisites

- Python 3.10+
- A virtual environment tool of your choice

## Install documentation dependencies

```bash
pip install -r requirements-docs.txt
```

## Run the docs locally

```bash
export PYTHONPATH="$PWD"
mkdocs serve
```

The documentation will be available at <http://127.0.0.1:8000/>.

## Build the docs

```bash
export PYTHONPATH="$PWD"
mkdocs build --strict
```
