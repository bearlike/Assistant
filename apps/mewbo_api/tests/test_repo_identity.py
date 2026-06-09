"""Unit tests for the RepoIdentity resolver (atomic class).

Covers URL/ref parsing across https / scp-git / host-less / bare forms,
normalization (lowercase host, strip ``.git``), the ``aliases()`` fan-out,
and reading a project path's git remotes via a stubbed subprocess.
"""

# mypy: ignore-errors

from __future__ import annotations

import pytest
from mewbo_api.repo_identity import RepoIdentity


class TestFromRemoteUrl:
    def test_https_url(self):
        ri = RepoIdentity.from_remote_url("https://github.com/bearlike/Assistant.git")
        assert ri == RepoIdentity(host="github.com", owner="bearlike", repo="Assistant")

    def test_https_url_without_suffix(self):
        ri = RepoIdentity.from_remote_url("https://git.hurricane.home/kk/Assistant")
        assert ri == RepoIdentity(host="git.hurricane.home", owner="kk", repo="Assistant")

    def test_scp_git_url(self):
        ri = RepoIdentity.from_remote_url("git@github.com:bearlike/Assistant.git")
        assert ri == RepoIdentity(host="github.com", owner="bearlike", repo="Assistant")

    def test_ssh_scheme_url(self):
        ri = RepoIdentity.from_remote_url("ssh://git@git.hurricane.home:2222/kk/Assistant.git")
        assert ri == RepoIdentity(host="git.hurricane.home", owner="kk", repo="Assistant")

    def test_host_is_lowercased(self):
        ri = RepoIdentity.from_remote_url("https://GitHub.COM/Bearlike/Assistant.git")
        assert ri.host == "github.com"
        # owner/repo case is preserved (only the host is normalized)
        assert ri.owner == "Bearlike"
        assert ri.repo == "Assistant"

    def test_hostless_owner_repo(self):
        ri = RepoIdentity.from_remote_url("bearlike/Assistant")
        assert ri == RepoIdentity(host="", owner="bearlike", repo="Assistant")

    def test_bare_repo(self):
        ri = RepoIdentity.from_remote_url("Assistant")
        assert ri == RepoIdentity(host="", owner="", repo="Assistant")

    def test_strips_git_suffix_on_bare(self):
        ri = RepoIdentity.from_remote_url("Assistant.git")
        assert ri.repo == "Assistant"

    def test_blank_returns_none(self):
        assert RepoIdentity.from_remote_url("") is None
        assert RepoIdentity.from_remote_url("   ") is None


class TestAliases:
    def test_full_remote_fans_out_three_forms(self):
        ri = RepoIdentity(host="github.com", owner="bearlike", repo="Assistant")
        aliases = ri.aliases()
        assert "github.com/bearlike/Assistant" in aliases
        assert "bearlike/Assistant" in aliases
        assert "Assistant" in aliases

    def test_hostless_has_two_forms(self):
        ri = RepoIdentity(host="", owner="bearlike", repo="Assistant")
        aliases = ri.aliases()
        assert "bearlike/Assistant" in aliases
        assert "Assistant" in aliases
        # no host segment present
        assert not any(a.startswith("/") for a in aliases)

    def test_bare_has_one_form(self):
        ri = RepoIdentity(host="", owner="", repo="Assistant")
        assert ri.aliases() == ["Assistant"]

    def test_aliases_have_no_duplicates(self):
        ri = RepoIdentity(host="github.com", owner="bearlike", repo="Assistant")
        aliases = ri.aliases()
        assert len(aliases) == len(set(aliases))


class TestCanonical:
    def test_canonical_is_host_owner_repo(self):
        ri = RepoIdentity(host="github.com", owner="bearlike", repo="Assistant")
        assert ri.canonical() == "github.com/bearlike/Assistant"


class TestForPath:
    def test_reads_remotes_via_subprocess(self, monkeypatch):
        captured = {}

        def fake_remotes(path):
            captured["path"] = path
            return [
                "https://github.com/bearlike/Assistant.git",
                "git@git.hurricane.home:kk/Assistant.git",
            ]

        monkeypatch.setattr(RepoIdentity, "_read_remote_urls", staticmethod(fake_remotes))
        identities = RepoIdentity.for_path("/some/repo")
        assert captured["path"] == "/some/repo"
        canon = {i.canonical() for i in identities}
        assert "github.com/bearlike/Assistant" in canon
        assert "git.hurricane.home/kk/Assistant" in canon

    def test_dedupes_identical_remotes(self, monkeypatch):
        monkeypatch.setattr(
            RepoIdentity,
            "_read_remote_urls",
            staticmethod(
                lambda path: [
                    "https://github.com/bearlike/Assistant.git",
                    "https://github.com/bearlike/Assistant.git",
                ]
            ),
        )
        identities = RepoIdentity.for_path("/some/repo")
        assert len(identities) == 1

    def test_no_remotes_returns_empty(self, monkeypatch):
        monkeypatch.setattr(
            RepoIdentity, "_read_remote_urls", staticmethod(lambda path: [])
        )
        assert RepoIdentity.for_path("/some/repo") == []

    def test_aliases_for_path_unions_all_remotes(self, monkeypatch):
        monkeypatch.setattr(
            RepoIdentity,
            "_read_remote_urls",
            staticmethod(
                lambda path: [
                    "https://github.com/bearlike/Assistant.git",
                    "git@git.hurricane.home:kk/Assistant.git",
                ]
            ),
        )
        aliases = RepoIdentity.aliases_for_path("/some/repo")
        assert "github.com/bearlike/Assistant" in aliases
        assert "git.hurricane.home/kk/Assistant" in aliases
        assert "bearlike/Assistant" in aliases
        assert "kk/Assistant" in aliases
        assert "Assistant" in aliases
        # no duplicates
        assert len(aliases) == len(set(aliases))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
