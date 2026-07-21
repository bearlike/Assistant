# email_organizer — the `submit_app` call

The two files under this directory (`app.py`, `pages/all_mail.py`) are the frontend.
`submit_app` reads them off disk; you pass only the metadata below. This is the whole
manifest that turns the files into a live app.

Each pipeline just declares its `schedule` — the platform arms it on the maintainer
session at submit time; the builder never calls a trigger tool itself.

```python
submit_app(
    app_id="email-organizer",
    title="Email Organizer",
    summary="Groups your inbox into actionable task clusters every morning.",
    icon="📥",
    workspace_ref={"kind": "own", "key": "email-organizer"},
    entrypoint="app.py",
    requirements=[],
    collections=[
        {
            "name": "emails",
            "description": "One fetched email.",
            "json_schema": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "sender": {"type": "string"},
                    "received_at": {"type": "string"},
                    "snippet": {"type": "string"},
                },
                "required": ["subject", "sender", "received_at"],
                "additionalProperties": False,
            },
        },
        {
            "name": "task_groups",
            "description": "Emails clustered into an actionable group (display-ready).",
            "json_schema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "count": {"type": "integer"},
                    "subjects": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["title", "count", "subjects"],
                "additionalProperties": False,
            },
        },
    ],
    pipelines=[
        {
            "name": "morning-organize",
            "wake_prompt": (
                "Fetch new emails via the workspace's email tools, upsert each into the "
                "`emails` collection (key = message id), then cluster them into actionable "
                "task_groups and upsert each group (key = a slug of its title). Use the "
                "app_data tool for every write and pass pipeline=\"morning-organize\"."
            ),
            "schedule": {"kind": "time.cron", "cron": "0 7 * * *"},   # platform-armed; on_demand: true for none
            "tools_allowlist": ["app_data", "read_file"],
            "cursor": {},
        },
    ],
    policies={"on_pipeline_failure": "repair", "max_docs_per_collection": 50000},
)
```

## Why the documents are display-ready

`app.py` renders `task_groups` documents directly — each already carries its `title`,
`count`, and a ready list of `subjects`. The frontend does no joining or aggregation
because v1 apps are read-only: the UI cannot call the agent back, so the
`morning-organize` pipeline leaves each collection in a shape the page can render as-is.
