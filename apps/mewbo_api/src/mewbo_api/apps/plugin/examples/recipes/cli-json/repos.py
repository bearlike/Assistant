import json


def parse_repositories(stdout: str) -> list[dict]:
    """Turn the CLI's declared JSON response into display-ready rows."""
    payload = json.loads(stdout)
    if not isinstance(payload, list):
        raise ValueError("forge CLI returned a JSON value other than a list")

    rows = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise ValueError(f"forge CLI item {index} is not an object")
        name = item.get("name")
        owner = item.get("owner", {}).get("login")
        url = item.get("html_url")
        if not all(isinstance(value, str) and value for value in (name, owner, url)):
            raise ValueError(f"forge CLI item {index} lacks name, owner, or html_url")
        rows.append({"name": name, "owner": owner, "url": url})
    return rows


def run(params: dict, ctx) -> list[dict]:
    response = ctx.exec(
        ["gh", "api", "--hostname", "github.com", "orgs/example/repos?per_page=20"]
    )
    if response["returncode"] != 0:
        raise RuntimeError(f"gh api failed: {response['stderr']}")
    return parse_repositories(response["stdout"])
