"""The I/O EDGE of the instruction-variable value catalog.

``InstructionContext.describe()`` (core) is PURE — it turns an
:class:`~mewbo_core.system_instructions.InstructionValueCatalog` of plain
tuples into the documented variable table. Somebody still has to go and *ask
this deployment* what its tools, capabilities, projects and models actually
are. That somebody is :class:`InstructionValueSources`, and it is the ONLY
class in this feature that performs I/O — the controller stays a wire adapter,
core stays a pure renderer.

**Failure isolation is the whole design, not a nicety.** ``GET
/api/system-instructions/variables`` is precisely what an operator opens when
their template is misbehaving — often *because* something in the deployment is
broken. A dead LiteLLM proxy (``LLMConfig.list_models`` raises ``ValueError``)
or a hanging MCP server during registry discovery must therefore degrade that
one row to an empty value list, never 500 the page that exists to debug it. So
every probe is:

* **independent** — resolved on its own thread, so one source cannot take the
  others down with it (and total latency is the slowest probe, not their sum);
* **bounded** — a wall-clock deadline covers the case a plain ``try/except``
  cannot: a probe that never returns at all;
* **abandonable, exactly once** — see :class:`SourceProbe`: a probe runs on a
  DAEMON thread (an abandoned one never blocks interpreter shutdown) and a
  source has at most ONE probe in flight (a hanging source costs one thread for
  the process, not one per request);
* **logged once, then degraded to ``()``** — never re-raised into the request.

Collaborators are injected as FIELDS (the house paradigm): the config accessor,
the managed-project store, the tool-registry loader, the plugin fan-out loader
and the agent-file parser. Nothing is reached for internally, so a test
simulates "the proxy is down" by injecting a config whose ``list_models``
raises — no monkeypatching, and no network in the suite.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mewbo_core.agent_registry import parse_agent_file
from mewbo_core.common import get_logger
from mewbo_core.config import get_config
from mewbo_core.plugins import load_all_plugin_components
from mewbo_core.system_instructions import InstructionValueCatalog
from mewbo_core.tool_registry import get_or_build_registry

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable

logging = get_logger(name="api.system_instructions.value_sources")


class SourceProbe:
    """ONE value source, resolved on its OWN daemon thread — single-flight.

    A fresh ``ThreadPoolExecutor`` per request carried neither property this
    class exists for:

    * **An abandoned worker LEAKED.** ``shutdown(wait=False,
      cancel_futures=True)`` only drops QUEUED futures, and every probe starts
      immediately, so it cancelled nothing. Pool workers are non-daemon and
      registered with ``concurrent.futures``' atexit join, so a probe wedged in
      MCP discovery kept its thread alive for the life of the process — one more
      per ``/variables`` call — and held interpreter shutdown open until SIGKILL.
      A daemon thread is abandonable for real.
    * **The wedge REPEATED.** Every request re-ran the doomed probe. Here a
      source has at most one probe in flight: a later caller joins the running
      one and shares its result, so a hanging source costs one thread in total,
      and the moment it recovers the next call probes it afresh.

    Degradation is unchanged and load-bearing: a raising source is logged once
    and read as ``()``, never re-raised into the request.
    """

    def __init__(self, name: str, resolve: Callable[[], tuple[str, ...]]) -> None:
        """Bind the source's *name* (it labels the thread and the logs) and its resolver."""
        self.name = name
        self.resolve = resolve
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._value: tuple[str, ...] = ()

    def start(self) -> None:
        """Probe the source — unless a probe of it is already in flight."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._value = ()
            self._thread = threading.Thread(
                target=self._run, name=f"instruction-values-{self.name}", daemon=True
            )
            self._thread.start()

    def settle(self, deadline: float) -> tuple[str, ...]:
        """This source's values, or ``()`` when it has not answered by *deadline*."""
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
            if thread.is_alive():
                logging.warning(
                    "instruction value source timed out, abandoned source={}", self.name
                )
                return ()
        with self._lock:
            return self._value

    def _run(self) -> None:
        """The thread body: resolve the source, or log the failure and stay empty.

        Catches ``Exception`` (not ``BaseException``): a broken source is data we
        degrade, but a ``KeyboardInterrupt``/``SystemExit`` unwinding the thread
        is not ours to swallow.
        """
        try:
            value = self.resolve()
        except Exception as exc:  # noqa: BLE001 - a dead source degrades, never 500s
            logging.warning(
                "instruction value source failed source={} error={}: {}",
                self.name,
                type(exc).__name__,
                exc,
            )
            return
        with self._lock:
            self._value = value


class InstructionValueSources:
    """Resolve this deployment's live facts into a pure ``InstructionValueCatalog``.

    One atomic class: the collaborators are state (injected fields), every
    probe is a method, and :meth:`catalog` is the one entry point. It returns
    plain data — the catalog crosses back into core's pure ``describe()``
    with no store, no client and no request in tow.
    """

    def __init__(
        self,
        *,
        project_store: Any,
        config_provider: Callable[[], Any] = get_config,
        registry_loader: Callable[..., Any] = get_or_build_registry,
        plugin_loader: Callable[[], Any] = load_all_plugin_components,
        agent_parser: Callable[[Path, str], Any] = parse_agent_file,
        # Sized so a COLD tools probe FITS, deliberately. That probe is live MCP
        # discovery across the configured projects: ~7.5s on a box with fifteen
        # servers, against ~0.2s once the shared registry cache is warm (the cache
        # is process-wide, so ordinary session traffic warms it too).
        #
        # A deadline UNDER the cold cost does not protect the operator. It just
        # makes the page prefer an EMPTY tools row (a lie, and precisely the lie
        # this feature exists to remove) over a slow one (the truth) — once per
        # process restart, at 6.0s it did exactly that. Better to make them wait
        # once, behind the pane's existing "Loading variables…" state, than to tell
        # them their deployment has no tools.
        #
        # Priming this at import was tried and REVERTED: the API is served by
        # gunicorn, which imports the module rather than calling main(), so the only
        # place a prime would fire in production is module scope — which also fires
        # it in every test that imports backend.py, putting real MCP network I/O
        # inside the suite. Not worth it to save one slow page load per restart.
        probe_timeout: float = 20.0,
        model_timeout: float = 4.0,
    ) -> None:
        """Bind the sources.

        *project_store* is the managed-project store ``GET /api/projects``
        already lists; *config_provider* yields the live ``AppConfig`` (its
        ``projects`` map and its ``llm`` block are two different sources that
        happen to share one accessor). *probe_timeout* is the wall-clock
        budget the whole fan-out shares; *model_timeout* is handed to
        ``LLMConfig.list_models`` so the HTTP call to the proxy is bounded at
        its own layer too.

        *registry_loader* defaults to the CACHED seam ``get_or_build_registry``
        — the very one ``Orchestrator`` builds every run's registry through, and
        keyed identically ``(cwd, extra_mcp_servers, mcp-config fingerprint)``.
        Uncached ``load_registry`` would make this page re-run live MCP discovery
        on every call, and (because the auto-manifest and the MCP pool are shared
        process-wide) a ``cwd=None`` load would rewrite the manifest and
        ``refresh_if_config_changed`` a project's servers straight off the pool.
        Sharing the cache means a scope a session already built costs a dict
        lookup here, and a scope this page builds first is warm for the session.
        """
        self.project_store = project_store
        self.config_provider = config_provider
        self.registry_loader = registry_loader
        self.plugin_loader = plugin_loader
        self.agent_parser = agent_parser
        self.probe_timeout = probe_timeout
        self.model_timeout = model_timeout
        # One long-lived probe per source: the single-flight guard (and the
        # thread it owns) has to OUTLIVE the request to bound a hanging source.
        self._probes = {
            name: SourceProbe(name, resolve)
            for name, resolve in (
                ("tools", self._tools),
                ("capabilities", self._capabilities),
                ("projects", self._projects),
                ("models", self._models),
            )
        }

    # -- the one entry point ------------------------------------------------

    def catalog(self) -> InstructionValueCatalog:
        """Every live value source, resolved concurrently under ONE shared deadline.

        ``surfaces``/``platforms`` are deliberately NOT passed: they are static
        knowledge core already owns (``KNOWN_SURFACES``/``KNOWN_PLATFORMS``),
        and re-deriving them here would give them a second home.
        """
        for probe in self._probes.values():
            probe.start()
        deadline = time.monotonic() + self.probe_timeout
        return InstructionValueCatalog(
            **{name: probe.settle(deadline) for name, probe in self._probes.items()}
        )

    # -- the probes ----------------------------------------------------------

    def _tools(self) -> tuple[str, ...]:
        """Every tool id an agent could hold: every scope's registry UNION the session tools.

        Must be a SUPERSET of what any one session reports in
        ``InstructionContext.tools`` (registry specs ∪ session tools), or the
        documented value list lies about a `{{ tools }}` membership test an
        operator writes against it. Three consequences of that, all deliberate:

        * disabled specs are INCLUDED (``include_disabled=True`` — a tool a
          deployment could turn on);
        * session tools are taken from the fan-out's aggregate rather than the
          capability-gated walk ``backend.py``'s ``Tools.get()`` does — a plugin
          with no ``requires-capabilities`` still contributes tool ids to a real
          session, so omitting them here would under-report;
        * the registry is loaded once per CWD a session can be scoped to, not
          only at ``cwd=None``. ``load_registry`` merges ``<cwd>/.mcp.json`` and
          the subtree's ``.mcp.json`` files, so a session bound to a configured
          project genuinely holds MCP tool ids a ``cwd=None`` catalog has never
          seen. Listing only the deployment-wide scope did not make the session's
          list "narrower" (which the ``known`` note already warns about) — it made
          it DIFFERENT, claiming ids the operator's own sessions hold cannot
          exist. See :meth:`_registry_cwds`.
        """
        fan_out = self.plugin_loader()
        extra_mcp_servers = fan_out.mcp_servers or None
        ids: set[str] = set()
        for cwd in self._registry_cwds():
            registry = self.registry_loader(cwd=cwd, extra_mcp_servers=extra_mcp_servers)
            ids.update(spec.tool_id for spec in registry.list_specs(include_disabled=True))
        ids.update(
            tool_id
            for entry in fan_out.session_tool_entries
            if (tool_id := str(entry.get("tool_id", "")).strip())
        )
        return tuple(sorted(ids))

    def _registry_cwds(self) -> list[str | None]:
        """The CWDs the tools catalog probes: deployment-wide, plus each CONFIGURED project.

        ``None`` is the deployment-wide scope (global ``mcp.json`` plus the plugin
        fan-out's servers). The configured projects are unioned on top because
        ``load_registry`` merges ``<cwd>/.mcp.json`` and the subtree's, so a
        session bound to one genuinely holds MCP tool ids a ``cwd=None`` catalog
        has never seen. Omitting them did not make a session's list "narrower"
        (which the ``known`` note already warns about), it made it DIFFERENT:
        the reference claimed ids the operator's own sessions hold cannot exist.

        **MANAGED projects are deliberately NOT probed, and that is a bound, not
        an oversight.** They are server-created rather than operator-declared, so
        the set is unbounded and churning: worktrees are cut per pickup and reaped
        at session end, and a long-lived store accretes promoted parents (a dev box
        here held 974, nearly all of them dead ``/tmp`` paths from old test runs).
        Probing them would put an O(store-size) fan of LIVE MCP discovery on the
        settings page an operator opens to debug a template, which is exactly the
        latency the shared deadline would then eat, silently emptying the tools row.
        Config projects are the operator's own declared, bounded, stable list, so
        they are the right thing to cover. The residue is DISCLOSED rather than
        hidden: ``InstructionValueCatalog`` says a tool reachable only through a
        managed project's own ``.mcp.json`` may be missing from the row. A `known`
        list with an accurate caveat is honest; an exhaustive-looking list that
        times out into emptiness is not.

        Non-existent paths are dropped: a stale ``app.json`` entry must not spend
        the deadline building a registry for a directory that is not there.
        """
        paths = {
            project.path
            for project in self.config_provider().projects.values()
            if project.path and Path(project.path).is_dir()
        }
        return [None, *sorted(paths)]

    def _capabilities(self) -> tuple[str, ...]:
        """The capability surface — COMPUTED, because no enum of it exists anywhere.

        A capability is only ever a string two sides agree on: a plugin
        manifest's ``requires-capabilities`` and an AgentDef's. So the honest
        set is their union, and it is derived here rather than hardcoded — a
        plugin that ships a new capability appears in the operator's value list
        the moment it is installed.

        Parsing the agent files directly (rather than building an
        ``AgentRegistry``) is not a shortcut: registration OVERLAYS the
        contributing plugin's manifest capabilities onto each agent
        (``AgentRegistry.register(..., capabilities=...)``), so the union over
        a built registry is exactly the union taken here — without standing up
        a registry the app has no other use for.
        """
        capabilities: set[str] = set()
        for component in self.plugin_loader().components:
            manifest = component.manifest
            if manifest is None:
                continue
            capabilities.update(manifest.requires_capabilities)
            for agent_file in component.agent_files:
                agent_def = self.agent_parser(Path(agent_file), f"plugin:{manifest.name}")
                if agent_def is not None:
                    capabilities.update(agent_def.requires_capabilities)
        return tuple(sorted(capabilities))

    def _projects(self) -> tuple[str, ...]:
        """Config-declared project names UNION managed ones — what ``GET /api/projects`` lists."""
        names = {name for name in self.config_provider().projects if name}
        names.update(
            project.name for project in self.project_store.list_projects() if project.name
        )
        return tuple(sorted(names))

    def _models(self) -> tuple[str, ...]:
        """Model names the configured LiteLLM proxy serves.

        The one probe that makes a live HTTP call — and the one most likely to
        be down when an operator is here debugging. ``list_models`` raises
        ``ValueError`` on an unreachable/erroring proxy; that becomes an empty
        row, not a 500 (see the module docstring).
        """
        return tuple(self.config_provider().llm.list_models(timeout=self.model_timeout))


__all__ = ["InstructionValueSources"]
