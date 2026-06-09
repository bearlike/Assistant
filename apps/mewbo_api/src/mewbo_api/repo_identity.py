"""Canonical git repository identity for a project (Gitea #43).

One atomic class, ``RepoIdentity`` — a frozen ``(host, owner, repo)`` triple
parsed from a git remote URL or a free-form reference. It lets the API match
an incoming project key (a name, a ``owner/repo``, a full ``host/owner/repo``,
or a bare repo name) against the projects it actually manages, and it enriches
``GET /api/projects`` with each project's git identity + every alias form it is
addressable by.

Parsing handles the four ref shapes Mewbo sees in the wild:

- ``https://host/owner/repo(.git)``        (HTTP/S clone URL)
- ``ssh://git@host:port/owner/repo.git``   (SSH-scheme clone URL)
- ``git@host:owner/repo.git``              (scp-like SSH shorthand)
- ``owner/repo`` / ``repo``                (host-less / bare reference)

The host is lowercased and a trailing ``.git`` is stripped; owner/repo case is
preserved (forge hosts are case-insensitive on host but path case can matter).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class RepoIdentity:
    """A normalized ``(host, owner, repo)`` view of one git remote.

    ``host`` and ``owner`` may be empty for host-less (``owner/repo``) and bare
    (``repo``) references respectively; ``repo`` is always present.
    """

    host: str
    owner: str
    repo: str

    # -- parsing -----------------------------------------------------------

    @staticmethod
    def _strip_git_suffix(value: str) -> str:
        return value[:-4] if value.endswith(".git") else value

    @classmethod
    def from_remote_url(cls, url: str) -> RepoIdentity | None:
        """Parse a remote URL or free-form ref into a ``RepoIdentity``.

        Returns ``None`` for a blank/whitespace-only input.
        """
        raw = (url or "").strip()
        if not raw:
            return None

        # scp-like SSH shorthand: ``git@host:owner/repo.git`` (no ``://``).
        if "://" not in raw and "@" in raw and ":" in raw:
            userhost, _, path = raw.partition(":")
            host = userhost.rpartition("@")[2]
            return cls._from_host_and_path(host, path)

        # Scheme URL: https://, ssh://, git://, http://.
        if "://" in raw:
            parts = urlsplit(raw)
            return cls._from_host_and_path(parts.hostname or "", parts.path)

        # Host-less reference: ``owner/repo`` or bare ``repo``.
        return cls._from_host_and_path("", raw)

    @classmethod
    def _from_host_and_path(cls, host: str, path: str) -> RepoIdentity:
        host = host.strip().lower()
        segments = [s for s in path.strip().strip("/").split("/") if s]
        if not segments:
            return cls(host=host, owner="", repo="")
        repo = cls._strip_git_suffix(segments[-1])
        owner = segments[-2] if len(segments) >= 2 else ""
        return cls(host=host, owner=owner, repo=repo)

    # -- addressing --------------------------------------------------------

    def canonical(self) -> str:
        """Return the most-specific addressable form for this identity."""
        return "/".join(p for p in (self.host, self.owner, self.repo) if p)

    def aliases(self) -> list[str]:
        """Return every form this repo is addressable by, most→least specific.

        ``host/owner/repo`` (when a host is known), ``owner/repo`` (when an
        owner is known), and the bare ``repo``. Order-preserving, de-duped.
        """
        forms: list[str] = []
        if self.host and self.owner and self.repo:
            forms.append(f"{self.host}/{self.owner}/{self.repo}")
        if self.owner and self.repo:
            forms.append(f"{self.owner}/{self.repo}")
        if self.repo:
            forms.append(self.repo)
        # Order-preserving dedupe (dict keys preserve insertion order).
        return list(dict.fromkeys(forms))

    # -- project-path reads ------------------------------------------------

    @staticmethod
    def _read_remote_urls(path: str) -> list[str]:
        """Return the unique remote fetch/push URLs configured at *path*.

        Best-effort: a non-repo path or a missing ``git`` binary yields an
        empty list (never raises).
        """
        try:
            proc = subprocess.run(
                ["git", "-C", path, "remote", "-v"],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        if proc.returncode != 0:
            return []
        urls: list[str] = []
        for line in proc.stdout.splitlines():
            # Format: ``<name>\t<url> (fetch|push)``
            parts = line.split()
            if len(parts) >= 2 and parts[1] not in urls:
                urls.append(parts[1])
        return urls

    @classmethod
    def for_path(cls, path: str) -> list[RepoIdentity]:
        """Return the distinct repo identities for the git repo at *path*."""
        identities: list[RepoIdentity] = []
        for url in cls._read_remote_urls(path):
            identity = cls.from_remote_url(url)
            if identity is not None and identity.repo and identity not in identities:
                identities.append(identity)
        return identities

    @classmethod
    def aliases_for_path(cls, path: str) -> list[str]:
        """Union the alias forms of every remote at *path* (order-preserving)."""
        forms: list[str] = []
        for identity in cls.for_path(path):
            forms.extend(identity.aliases())
        return list(dict.fromkeys(forms))
