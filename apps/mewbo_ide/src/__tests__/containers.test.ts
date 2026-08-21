import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { BrokerConfig } from "../config.js";
import type { BrokerLogger, ContainerSpec } from "../containers.js";
import { IdeContainers, KIND_LABEL, KIND_VALUE, SESSION_LABEL, WATCHDOG_CMD } from "../containers.js";
import { DeadlineFiles } from "../deadlines.js";
import { CODE_SERVER_SETTINGS_PATH, DefaultSettingsFile } from "../default_settings.js";
import { BrokerError } from "../errors.js";
import { DockerNotFound, DockerUnreachable, FakeDockerClient } from "./fakeDocker.js";

const SID = "a1b2c3d4e5f60718293a4b5c6d7e8f90";
const OTHER_SID = "0f1e2d3c4b5a69788796a5b4c3d2e1f0";
const NOW = new Date("2026-01-01T00:00:00.000Z");

let tmp: string;
let stateDir: string;
let deadlines: DeadlineFiles;
let settings: DefaultSettingsFile;
let docker: FakeDockerClient;
let containers: IdeContainers;
let logs: string[];

const logger = (): BrokerLogger => ({
  info: (message) => logs.push(message),
  warn: (message) => logs.push(message),
});

const configFor = (overrides: Record<string, string> = {}): BrokerConfig =>
  BrokerConfig.fromEnv({
    MEWBO_IDE_BROKER_TOKEN: "0123456789abcdef0123",
    MEWBO_IDE_ALLOWED_ROOTS: "/srv/workspaces",
    MEWBO_IDE_STATE_DIR: stateDir,
    MEWBO_IDE_IMAGE: "codercom/code-server:4.99.1",
    MEWBO_IDE_NETWORK: "mewbo-ide",
    MEWBO_IDE_MEMORY: "2g",
    MEWBO_IDE_CPUS: "1.5",
    MEWBO_IDE_PIDS_LIMIT: "256",
    ...overrides,
  });

beforeEach(() => {
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), "mewbo-ide-containers-"));
  stateDir = path.join(tmp, "state");
  deadlines = new DeadlineFiles(stateDir);
  settings = new DefaultSettingsFile(stateDir);
  docker = new FakeDockerClient();
  logs = [];
  containers = new IdeContainers(docker, configFor(), deadlines, settings, logger(), () => NOW);
});

afterEach(() => {
  fs.rmSync(tmp, { recursive: true, force: true });
});

describe("the watchdog command", () => {
  it("matches the shell string the Python IdeManager has always used", () => {
    expect(WATCHDOG_CMD).toBe(
      "(while [ $(date +%s) -lt $(cat /mewbo/deadline) ]; do sleep 15; done; " +
        "kill 1) & exec /usr/bin/entrypoint.sh --auth password " +
        "--bind-addr 0.0.0.0:8080 --disable-telemetry --disable-update-check " +
        "--abs-proxy-base-path /ide/{sid} /home/coder/project",
    );
  });

  it("stays byte-identical to the Python WATCHDOG_CMD constant on disk", () => {
    // Read the other side rather than trusting a retyped copy: this string is a
    // cross-language contract, and a single changed space silently changes what
    // runs as PID 1 inside every IDE container.
    //
    // A mirror of this check lives in tests/test_ide_watchdog_parity.py. The
    // pair is NOT redundant: the two workflows are path-filtered to disjoint
    // trees, so each guards the direction the other cannot see. An edit to
    // apps/mewbo_ide/** runs mewbo-ide.yml (this test) but not coverage.yml;
    // an edit to apps/mewbo_api/** runs coverage.yml (the python test) but not
    // mewbo-ide.yml. Delete either and one direction of drift ships unguarded.
    const pythonPath = new URL("../../../mewbo_api/src/mewbo_api/ide.py", import.meta.url);
    const source = fs.readFileSync(pythonPath, "utf8");
    const block = /WATCHDOG_CMD\s*=\s*\(([\s\S]*?)\n\)/.exec(source);
    expect(block, "WATCHDOG_CMD assignment not found in mewbo_api/ide.py").not.toBeNull();
    const fragments = [...(block?.[1] ?? "").matchAll(/"((?:[^"\\]|\\.)*)"/g)].map(
      (match) => match[1] ?? "",
    );
    expect(fragments.length).toBeGreaterThan(0);
    expect(WATCHDOG_CMD).toBe(fragments.join(""));
  });

  it("interpolates the session id into the proxy base path", () => {
    expect(IdeContainers.watchdogCommand(SID)).toContain(`--abs-proxy-base-path /ide/${SID} `);
    expect(IdeContainers.watchdogCommand(SID)).not.toContain("{sid}");
  });
});

describe("IdeContainers.buildSpec", () => {
  const spec = (): ContainerSpec =>
    containers.buildSpec({
      sessionId: SID,
      workspacePath: "/srv/workspaces/project-a",
      password: "s3cret-token_ABCDEFGH",
      createdAt: NOW,
      expiresAt: new Date(NOW.getTime() + 3600_000),
    });

  it("derives the container name from the session id", () => {
    expect(spec().name).toBe(`mewbo-ide-${SID}`);
    expect(IdeContainers.nameFor(SID)).toBe(`mewbo-ide-${SID}`);
  });

  it("takes the image, network and limits from config, never from the caller", () => {
    const built = spec();
    expect(built.Image).toBe("codercom/code-server:4.99.1");
    expect(built.HostConfig.NetworkMode).toBe("mewbo-ide");
    expect(built.HostConfig.Memory).toBe(2 * 1024 * 1024 * 1024);
    expect(built.HostConfig.NanoCpus).toBe(1_500_000_000);
    expect(built.HostConfig.PidsLimit).toBe(256);
    expect(built.HostConfig.AutoRemove).toBe(false);
  });

  it("binds the workspace, the deadline file and the seeded settings file", () => {
    expect(spec().HostConfig.Binds).toEqual([
      "/srv/workspaces/project-a:/home/coder/project:rw",
      `${path.join(stateDir, `${SID}.deadline`)}:/mewbo/deadline:ro`,
      `${settings.hostPath()}:${CODE_SERVER_SETTINGS_PATH}:ro`,
    ]);
  });

  it("mounts nothing by volume when no volume root is configured", () => {
    expect(spec().HostConfig.Mounts).toEqual([]);
  });

  it("puts the password in the one env var the broker chooses", () => {
    expect(spec().Env).toEqual(["PASSWORD=s3cret-token_ABCDEFGH"]);
  });

  it("labels the container for the sweep", () => {
    expect(spec().Labels).toEqual({
      [KIND_LABEL]: KIND_VALUE,
      [SESSION_LABEL]: SID,
      "mewbo.created_at": "2026-01-01T00:00:00.000Z",
      "mewbo.expires_at": "2026-01-01T01:00:00.000Z",
    });
  });

  it("runs the watchdog as the entrypoint with the session id interpolated", () => {
    expect(spec().Entrypoint).toEqual(["sh", "-c", WATCHDOG_CMD.replace("{sid}", SID)]);
  });
});

describe("IdeContainers.buildSpec — volume-backed workspaces", () => {
  // A path under /nonexistent-volume-root never needs to exist on disk:
  // `canonicalRoot` falls back to `path.normalize` when `realpath` fails,
  // exactly the way an as-yet-unmounted `MEWBO_IDE_ALLOWED_ROOTS` entry does.
  const VOLUME_ROOT = "/nonexistent-volume-root/wiki-clones";
  const VOLUME_NAME = "assistant_wiki-clones";

  const withVolumeRoots = (): IdeContainers =>
    new IdeContainers(
      docker,
      configFor({ MEWBO_IDE_VOLUME_ROOTS: `${VOLUME_ROOT}=${VOLUME_NAME}` }),
      deadlines,
      settings,
      logger(),
      () => NOW,
    );

  it("mounts a workspace under a configured volume root BY VOLUME, with a subpath, not a bind", () => {
    const built = withVolumeRoots().buildSpec({
      sessionId: SID,
      workspacePath: `${VOLUME_ROOT}/some-repo`,
      password: "s3cret-token_ABCDEFGH",
      createdAt: NOW,
      expiresAt: new Date(NOW.getTime() + 3600_000),
    });

    expect(built.HostConfig.Mounts).toEqual([
      {
        Type: "volume",
        Source: VOLUME_NAME,
        Target: "/home/coder/project",
        ReadOnly: false,
        VolumeOptions: { Subpath: "some-repo" },
      },
    ]);
    // No workspace BIND — the point of the whole feature. Only the deadline
    // file and the seeded settings file remain as binds.
    expect(built.HostConfig.Binds).toEqual([
      `${path.join(stateDir, `${SID}.deadline`)}:/mewbo/deadline:ro`,
      `${settings.hostPath()}:${CODE_SERVER_SETTINGS_PATH}:ro`,
    ]);
  });

  it("omits Subpath when the workspace IS the volume root", () => {
    const built = withVolumeRoots().buildSpec({
      sessionId: SID,
      workspacePath: VOLUME_ROOT,
      password: "s3cret-token_ABCDEFGH",
      createdAt: NOW,
      expiresAt: new Date(NOW.getTime() + 3600_000),
    });

    expect(built.HostConfig.Mounts).toEqual([
      {
        Type: "volume",
        Source: VOLUME_NAME,
        Target: "/home/coder/project",
        ReadOnly: false,
      },
    ]);
  });

  it("falls back to a bind for a workspace outside every configured volume root", () => {
    const built = withVolumeRoots().buildSpec({
      sessionId: SID,
      workspacePath: "/srv/workspaces/project-a",
      password: "s3cret-token_ABCDEFGH",
      createdAt: NOW,
      expiresAt: new Date(NOW.getTime() + 3600_000),
    });

    expect(built.HostConfig.Mounts).toEqual([]);
    expect(built.HostConfig.Binds[0]).toBe("/srv/workspaces/project-a:/home/coder/project:rw");
  });

  it("refuses a subpath that would escape its configured volume root", () => {
    // Textually this starts with `${VOLUME_ROOT}/`, so a naive prefix check
    // would accept it — `path.relative` resolves the embedded `..` and
    // reveals the escape, which is exactly why the check is done that way
    // rather than with `startsWith` alone.
    expect(() =>
      withVolumeRoots().buildSpec({
        sessionId: SID,
        workspacePath: `${VOLUME_ROOT}/../escape`,
        password: "s3cret-token_ABCDEFGH",
        createdAt: NOW,
        expiresAt: new Date(NOW.getTime() + 3600_000),
      }),
    ).toThrowError(/escapes its configured volume root/);
  });
});

describe("IdeContainers.create", () => {
  const inputs = {
    sessionId: SID,
    workspacePath: "/srv/workspaces/project-a",
    password: "s3cret-token_ABCDEFGH",
    createdAt: NOW,
    expiresAt: new Date(NOW.getTime() + 3600_000),
  };

  it("force-removes a lingering container before creating, so the name cannot conflict", async () => {
    docker.seed({
      Id: "stale",
      Names: [`/mewbo-ide-${SID}`],
      State: "exited",
      Labels: { [KIND_LABEL]: KIND_VALUE, [SESSION_LABEL]: SID },
    });
    const name = await containers.create(inputs);
    expect(name).toBe(`mewbo-ide-${SID}`);
    expect(docker.removed).toEqual([`mewbo-ide-${SID}`]);
    expect(docker.created).toHaveLength(1);
    expect(docker.started).toEqual([`id-mewbo-ide-${SID}`]);
  });

  it("creates cleanly when nothing is there", async () => {
    await containers.create(inputs);
    expect(docker.removed).toEqual([]);
    expect(docker.created[0]?.name).toBe(`mewbo-ide-${SID}`);
  });

  it("maps an unreachable daemon to a retryable 503", async () => {
    docker.createFailure = new DockerUnreachable();
    const failure = await containers.create(inputs).catch((err: unknown) => err);
    expect(failure).toBeInstanceOf(BrokerError);
    expect((failure as BrokerError).status).toBe(503);
    expect((failure as BrokerError).code).toBe("docker_unavailable");
    expect((failure as BrokerError).retryable).toBe(true);
  });

  it("maps a daemon refusal to a non-retryable 502", async () => {
    const conflict = Object.assign(new Error("conflict: name already in use"), {
      statusCode: 409,
    });
    docker.createFailure = conflict;
    const failure = await containers.create(inputs).catch((err: unknown) => err);
    expect((failure as BrokerError).status).toBe(502);
    expect((failure as BrokerError).code).toBe("docker_error");
  });
});

describe("IdeContainers.ensureImage", () => {
  const inputs = {
    sessionId: SID,
    workspacePath: "/srv/workspaces/project-a",
    password: "s3cret-token_ABCDEFGH",
    createdAt: NOW,
    expiresAt: new Date(NOW.getTime() + 3600_000),
  };

  it("does not pull when the daemon already has the image", async () => {
    docker.imagePresent = true;
    await containers.create(inputs);
    expect(docker.pulled).toEqual([]);
  });

  it("pulls the configured image when the daemon has never seen it", async () => {
    docker.imagePresent = false;
    await containers.create(inputs);
    expect(docker.pulled).toEqual(["codercom/code-server:4.99.1"]);
    // The create/start that follows must see the pull as already done.
    expect(docker.created).toHaveLength(1);
  });

  it("logs the pull so a cold-image launch is diagnosable, not just slow", async () => {
    docker.imagePresent = false;
    await containers.create(inputs);
    expect(logs.some((line) => line.includes("pulling missing image"))).toBe(true);
    expect(logs.some((line) => line.includes("pulled codercom/code-server:4.99.1"))).toBe(true);
  });

  it("maps a pull failure through the same daemon-refusal classification as create", async () => {
    docker.imagePresent = false;
    docker.pullFailure = Object.assign(new Error("manifest unknown"), { statusCode: 404 });
    const failure = await containers.create(inputs).catch((err: unknown) => err);
    expect(failure).toBeInstanceOf(BrokerError);
    expect((failure as BrokerError).status).toBe(502);
    expect((failure as BrokerError).code).toBe("docker_error");
  });

  it("is safe to call directly, independent of create — the boot-time preflight path", async () => {
    docker.imagePresent = false;
    await containers.ensureImage();
    expect(docker.pulled).toEqual(["codercom/code-server:4.99.1"]);
    docker.pulled.length = 0;
    await containers.ensureImage();
    expect(docker.pulled).toEqual([]);
  });
});

describe("IdeContainers.inspect and remove", () => {
  it("reports absent, running and exited", async () => {
    expect(await containers.inspect(SID)).toBe("absent");
    docker.seed({ Id: "x", Names: [`/mewbo-ide-${SID}`], State: "running", Labels: {} });
    expect(await containers.inspect(SID)).toBe("running");
    docker.containers = [{ Id: "x", Names: [`/mewbo-ide-${SID}`], State: "exited", Labels: {} }];
    expect(await containers.inspect(SID)).toBe("exited");
  });

  it("reports whether a container existed on remove", async () => {
    expect(await containers.remove(SID)).toBe(false);
    docker.seed({ Id: "x", Names: [`/mewbo-ide-${SID}`], State: "running", Labels: {} });
    expect(await containers.remove(SID)).toBe(true);
  });

  it("swallows a daemon failure in removeBestEffort so teardown still completes", async () => {
    const broken = new FakeDockerClient();
    broken.getContainer = (): never => {
      throw new DockerUnreachable();
    };
    const withBrokenDaemon = new IdeContainers(
      broken,
      configFor(),
      deadlines,
      settings,
      logger(),
      () => NOW,
    );
    await expect(withBrokenDaemon.remove(SID)).rejects.toBeInstanceOf(BrokerError);
    await expect(withBrokenDaemon.removeBestEffort(SID)).resolves.toBe(false);
  });

  it("treats a 404 as absent rather than an error", async () => {
    expect(new DockerNotFound("x").statusCode).toBe(404);
    expect(await containers.inspect(OTHER_SID)).toBe("absent");
  });
});

describe("IdeContainers.sweep", () => {
  const seedProxy = (): void =>
    docker.seed({
      // The real nginx reverse proxy. It matches the `mewbo-ide-` name prefix,
      // which is exactly why the sweep filters on the label instead.
      Id: "proxy",
      Names: ["/mewbo-ide-proxy"],
      State: "running",
      Labels: {},
    });

  const seedIde = (id: string, sessionId: string, state: string): void =>
    docker.seed({
      Id: id,
      Names: [`/mewbo-ide-${sessionId}`],
      State: state,
      Labels: { [KIND_LABEL]: KIND_VALUE, [SESSION_LABEL]: sessionId },
    });

  it("asks the daemon for the label, not the name prefix", async () => {
    await containers.sweep();
    expect(docker.listCalls[0]).toEqual({
      all: true,
      filters: { label: [`${KIND_LABEL}=${KIND_VALUE}`] },
    });
  });

  it("removes an exited labelled container", async () => {
    seedIde("exited", SID, "exited");
    const summary = await containers.sweep();
    expect(docker.removed).toEqual([`mewbo-ide-${SID}`]);
    expect(summary.removed).toBe(1);
  });

  it("removes a running labelled container whose deadline has passed", async () => {
    seedIde("expired", SID, "running");
    deadlines.write(SID, new Date(NOW.getTime() - 1000));
    await containers.sweep();
    expect(docker.removed).toEqual([`mewbo-ide-${SID}`]);
  });

  it("removes a running labelled container with no deadline file", async () => {
    seedIde("orphan", SID, "running");
    await containers.sweep();
    expect(docker.removed).toEqual([`mewbo-ide-${SID}`]);
  });

  it("LEAVES a running labelled container whose deadline is ahead", async () => {
    seedIde("healthy", SID, "running");
    deadlines.write(SID, new Date(NOW.getTime() + 3600_000));
    const summary = await containers.sweep();
    expect(docker.removed).toEqual([]);
    expect(summary).toEqual({ inspected: 1, removed: 0 });
  });

  it("never touches a container lacking the mewbo.kind=web-ide label", async () => {
    seedProxy();
    seedIde("expired", SID, "exited");
    await containers.sweep();
    expect(docker.removed).toEqual([`mewbo-ide-${SID}`]);
    expect(docker.containers.map((c) => c.Id)).toContain("proxy");
  });

  it("still spares mewbo-ide-proxy when the daemon ignores the label filter", async () => {
    // Defence in depth: reaping the proxy takes the whole feature down, so the
    // sweep re-checks the label per item instead of trusting the filter.
    docker.honorFilters = false;
    seedProxy();
    seedIde("expired", SID, "exited");
    const summary = await containers.sweep();
    expect(docker.removed).toEqual([`mewbo-ide-${SID}`]);
    expect(docker.containers.map((c) => c.Id)).toContain("proxy");
    expect(summary.inspected).toBe(1);
  });

  it("logs one line per action plus a summary", async () => {
    seedIde("expired", SID, "exited");
    seedIde("healthy", OTHER_SID, "running");
    deadlines.write(OTHER_SID, new Date(NOW.getTime() + 3600_000));
    await containers.sweep();
    expect(logs.filter((line) => line.includes("sweep removed"))).toHaveLength(1);
    expect(logs.filter((line) => line.includes("sweep keeping"))).toHaveLength(1);
    expect(logs.filter((line) => line.includes("sweep complete"))).toHaveLength(1);
  });
});
