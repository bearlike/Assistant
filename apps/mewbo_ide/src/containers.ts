import path from "node:path";

import { canonicalRoot } from "./allowlist.js";
import type { BrokerConfig, VolumeRoot } from "./config.js";
import type { DeadlineFiles } from "./deadlines.js";
import type { DefaultSettingsFile } from "./default_settings.js";
import { CODE_SERVER_SETTINGS_PATH } from "./default_settings.js";
import { BrokerError } from "./errors.js";

/**
 * The container watchdog, byte-identical to the `WATCHDOG_CMD` constant the
 * Python `IdeManager` has always used. Split across the same five fragments so
 * the two can be diffed line by line.
 *
 * `{sid}` is interpolated from a value that MUST have matched
 * `^[a-f0-9]{32}$` before reaching here — there is no quoting in this string
 * and none is wanted, because the only safe input is one with no shell
 * metacharacters at all.
 */
export const WATCHDOG_CMD =
  "(while [ $(date +%s) -lt $(cat /mewbo/deadline) ]; do sleep 15; done; " +
  "kill 1) & " +
  "exec /usr/bin/entrypoint.sh --auth password --bind-addr 0.0.0.0:8080 " +
  "--disable-telemetry --disable-update-check " +
  "--abs-proxy-base-path /ide/{sid} /home/coder/project";

export const KIND_LABEL = "mewbo.kind";
export const KIND_VALUE = "web-ide";
export const SESSION_LABEL = "mewbo.session_id";

/**
 * Errno values that mean "the daemon was never reached". These map to a
 * retryable 503; anything else the daemon itself answered maps to 502.
 */
const UNREACHABLE_ERRNOS = new Set([
  "ENOENT",
  "ECONNREFUSED",
  "EACCES",
  "EPIPE",
  "ETIMEDOUT",
  "ECONNRESET",
  "EHOSTUNREACH",
]);

/** `absent` is a status, never an error — see the status route's contract. */
export type IdeStatus = "absent" | "running" | "exited";

/**
 * A volume-backed mount, used instead of a bind when the workspace resolves
 * under a configured `MEWBO_IDE_VOLUME_ROOTS` entry — the source lives in a
 * docker named volume, not on the host filesystem, so a bind `Source` string
 * naming that host path would mount whatever the daemon's HOST happens to
 * have at that path, which need not be (and in the motivating case, was NOT)
 * the volume's content.
 */
export interface VolumeMount {
  Type: "volume";
  Source: string;
  Target: string;
  ReadOnly: boolean;
  /** Omitted (not empty-string) when the workspace IS the volume root. */
  VolumeOptions?: { Subpath: string };
}

/** The exact shape handed to the docker daemon. Constructed here, never accepted. */
export interface ContainerSpec {
  name: string;
  Image: string;
  Entrypoint: string[];
  Env: string[];
  Labels: Record<string, string>;
  HostConfig: {
    Binds: string[];
    /** Empty unless the workspace fell under a configured volume root. */
    Mounts: VolumeMount[];
    NetworkMode: string;
    Memory: number;
    NanoCpus: number;
    PidsLimit: number;
    AutoRemove: boolean;
  };
}

export interface SpecInputs {
  sessionId: string;
  /** Already realpath-resolved and allowlist-checked. */
  workspacePath: string;
  password: string;
  createdAt: Date;
  expiresAt: Date;
}

export interface DockerContainerHandle {
  start(): Promise<unknown>;
  remove(options: { force: boolean }): Promise<unknown>;
  inspect(): Promise<{ State?: { Status?: string } }>;
}

export interface DockerListItem {
  Id: string;
  Names?: string[];
  State?: string;
  Labels?: Record<string, string>;
}

export interface DockerListOptions {
  all: boolean;
  filters?: { label?: string[] };
}

/** The slice of dockerode this service uses. A test injects a fake of exactly this. */
export interface DockerClientLike {
  listContainers(options: DockerListOptions): Promise<DockerListItem[]>;
  getContainer(id: string): DockerContainerHandle;
  createContainer(spec: ContainerSpec): Promise<DockerContainerHandle>;
  /** Whether `image` already has a local layer, without pulling it. */
  imageExists(image: string): Promise<boolean>;
  /** Pull `image`, resolving only once the pull has actually finished. */
  pullImage(image: string): Promise<void>;
}

export interface BrokerLogger {
  info(message: string): void;
  warn(message: string): void;
}

export interface SweepSummary {
  inspected: number;
  removed: number;
}

/**
 * Owns the docker handle and every container decision.
 *
 * The load-bearing rule of this whole service lives in `buildSpec`: image,
 * binds, network, limits, entrypoint, labels and the container NAME all come
 * from `BrokerConfig`. Exactly three caller-supplied values reach a container —
 * the password (into an env var the broker chooses), the resolved workspace
 * path (into a bind OR volume source the broker chooses, per
 * `resolveWorkspaceMount` — which mount kind, and which volume, is decided
 * entirely by broker config), and the deadline epoch (into a file the broker
 * writes). Nothing else the API sends can influence the spec, because nothing
 * else is read.
 */
export class IdeContainers {
  private readonly docker: DockerClientLike;
  private readonly config: BrokerConfig;
  private readonly deadlines: DeadlineFiles;
  private readonly settings: DefaultSettingsFile;
  private readonly log: BrokerLogger;
  private readonly now: () => Date;
  /**
   * `config.volumeRoots`, canonicalized the identical way
   * `WorkspaceAllowlist` canonicalizes the same strings (see `canonicalRoot`'s
   * doc comment) — computed once, here, rather than per `buildSpec` call,
   * since it needs a `realpath` and never changes after boot.
   */
  private readonly volumeRoots: readonly VolumeRoot[];

  constructor(
    docker: DockerClientLike,
    config: BrokerConfig,
    deadlines: DeadlineFiles,
    settings: DefaultSettingsFile,
    log: BrokerLogger,
    now: () => Date,
  ) {
    this.docker = docker;
    this.config = config;
    this.deadlines = deadlines;
    this.settings = settings;
    this.log = log;
    this.now = now;
    this.volumeRoots = config.volumeRoots.map((volumeRoot) => ({
      root: canonicalRoot(volumeRoot.root),
      volume: volumeRoot.volume,
    }));
  }

  /** The container name is DERIVED from the session id, never accepted. */
  static nameFor(sessionId: string): string {
    return `mewbo-ide-${sessionId}`;
  }

  static watchdogCommand(sessionId: string): string {
    return WATCHDOG_CMD.replace("{sid}", sessionId);
  }

  /**
   * Construct the spec. Pure: no daemon, no filesystem, no clock.
   *
   * Cost: O(1).
   */
  buildSpec(inputs: SpecInputs): ContainerSpec {
    const binds: string[] = [];
    const mounts: VolumeMount[] = [];
    const workspaceMount = this.resolveWorkspaceMount(inputs.workspacePath);
    if (workspaceMount === null) {
      // No configured volume root claims this path: today's behavior,
      // unchanged — a host bind whose source the broker's own containment
      // check already resolved and verified.
      binds.push(`${inputs.workspacePath}:/home/coder/project:rw`);
    } else {
      mounts.push(workspaceMount);
    }
    binds.push(`${this.deadlines.pathFor(inputs.sessionId)}:/mewbo/deadline:ro`);
    binds.push(`${this.settings.hostPath()}:${CODE_SERVER_SETTINGS_PATH}:ro`);

    return {
      name: IdeContainers.nameFor(inputs.sessionId),
      Image: this.config.image,
      Entrypoint: ["sh", "-c", IdeContainers.watchdogCommand(inputs.sessionId)],
      Env: [`PASSWORD=${inputs.password}`],
      Labels: {
        [KIND_LABEL]: KIND_VALUE,
        [SESSION_LABEL]: inputs.sessionId,
        "mewbo.created_at": inputs.createdAt.toISOString(),
        "mewbo.expires_at": inputs.expiresAt.toISOString(),
      },
      HostConfig: {
        Binds: binds,
        Mounts: mounts,
        NetworkMode: this.config.network,
        Memory: this.config.memoryBytes(),
        NanoCpus: this.config.nanoCpus(),
        PidsLimit: this.config.pidsLimit,
        AutoRemove: false,
      },
    };
  }

  /**
   * Return a volume mount for `workspacePath`, or null when no configured
   * volume root claims it — the ordinary bind-mount path.
   *
   * `workspacePath` arrives already `realpath`-resolved and containment-
   * checked by `WorkspaceAllowlist` (see `SpecInputs.workspacePath`'s own
   * doc), which is unioned with these same volume roots — so by the time this
   * runs, a matching root's subpath cannot actually escape. The check stays
   * here anyway: a daemon-level refusal is not diagnosable the way a named
   * `BrokerError` is (see the class doc on 502 vs 503), and `buildSpec` is
   * also exercised directly by tests with no allowlist in front of it.
   */
  private resolveWorkspaceMount(workspacePath: string): VolumeMount | null {
    for (const volumeRoot of this.volumeRoots) {
      const isRootItself = workspacePath === volumeRoot.root;
      const prefix = volumeRoot.root.endsWith(path.sep)
        ? volumeRoot.root
        : `${volumeRoot.root}${path.sep}`;
      if (!isRootItself && !workspacePath.startsWith(prefix)) {
        continue;
      }
      const subpath = path.relative(volumeRoot.root, workspacePath);
      if (subpath === ".." || subpath.startsWith(`..${path.sep}`) || path.isAbsolute(subpath)) {
        throw BrokerError.workspaceDenied(
          `workspace_path escapes its configured volume root: ${workspacePath}`,
        );
      }
      return {
        Type: "volume",
        Source: volumeRoot.volume,
        Target: "/home/coder/project",
        ReadOnly: false,
        ...(subpath.length > 0 ? { VolumeOptions: { Subpath: subpath } } : {}),
      };
    }
    return null;
  }

  /**
   * Create-or-replace, returning the container name.
   *
   * The existing container is force-removed first because the name is
   * deterministic: a lingering exited container would 409 the create on a name
   * conflict, which is precisely the state a re-open needs to recover from.
   *
   * Cost: normally O(1) — four daemon round trips, independent of how many
   * containers exist. The exception is a cold image: `ensureImage` then pays
   * a real network pull, once, until the layer is cached locally.
   */
  async create(inputs: SpecInputs): Promise<string> {
    await this.remove(inputs.sessionId);
    await this.ensureImage();
    const spec = this.buildSpec(inputs);
    const container = await this.call(`create ${spec.name}`, () =>
      this.docker.createContainer(spec),
    );
    try {
      await this.call(`start ${spec.name}`, () => container.start());
    } catch (err) {
      // A created-but-unstarted container would block the next create on its
      // name, so the failure must not also leave the wreckage behind.
      await this.removeBestEffort(inputs.sessionId);
      throw err;
    }
    this.log.info(`ide-broker: started ${spec.name} until ${inputs.expiresAt.toISOString()}`);
    return spec.name;
  }

  /**
   * Pull `config.image` if the daemon does not already have it.
   *
   * `docker.createContainer` — unlike the `docker run` CLI — never pulls a
   * missing image implicitly; the daemon just answers `create` with a 404
   * "No such image". A host that has never run this image (a fresh
   * deployment, or an operator who bumped `MEWBO_IDE_IMAGE`) failed every
   * launch that way until something happened to `docker pull` it by hand.
   * Checked first so the common case — the image already cached — costs one
   * cheap inspect rather than a registry round trip on every launch.
   * Idempotent and safe to call from both `create` (self-heals if the image
   * was pruned after boot) and the broker's own startup, alongside `sweep`.
   */
  async ensureImage(): Promise<void> {
    const image = this.config.image;
    const present = await this.call(`inspect image ${image}`, () =>
      this.docker.imageExists(image),
    );
    if (present) {
      return;
    }
    this.log.info(`ide-broker: pulling missing image ${image}`);
    await this.call(`pull ${image}`, () => this.docker.pullImage(image));
    this.log.info(`ide-broker: pulled ${image}`);
  }

  /** Cost: O(1). */
  async inspect(sessionId: string): Promise<IdeStatus> {
    const name = IdeContainers.nameFor(sessionId);
    try {
      const info = await this.docker.getContainer(name).inspect();
      return info.State?.Status === "running" ? "running" : "exited";
    } catch (err) {
      if (IdeContainers.isNotFound(err)) {
        return "absent";
      }
      throw IdeContainers.dockerFailure(`inspect ${name}`, err);
    }
  }

  /**
   * Force-remove by session id. Returns whether a container existed.
   *
   * Raises on a daemon failure — the create path depends on this actually
   * having happened. Use `removeBestEffort` where a failure must not surface.
   */
  async remove(sessionId: string): Promise<boolean> {
    const name = IdeContainers.nameFor(sessionId);
    try {
      await this.docker.getContainer(name).remove({ force: true });
      return true;
    } catch (err) {
      if (IdeContainers.isNotFound(err)) {
        return false;
      }
      throw IdeContainers.dockerFailure(`remove ${name}`, err);
    }
  }

  /**
   * Force-remove, swallowing every failure.
   *
   * The teardown path must still unlink the deadline file and answer the caller
   * when the daemon is down; a docker outage must not strand the API holding
   * state it can never clear.
   */
  async removeBestEffort(sessionId: string): Promise<boolean> {
    try {
      return await this.remove(sessionId);
    } catch (err) {
      const reason = err instanceof Error ? err.message : String(err);
      this.log.warn(`ide-broker: could not remove ${IdeContainers.nameFor(sessionId)}: ${reason}`);
      return false;
    }
  }

  /**
   * Reap containers stranded by a restart: exited, deadline-less, or expired.
   *
   * Filtered by the `mewbo.kind=web-ide` LABEL, never by the `mewbo-ide-` name
   * prefix — the nginx reverse proxy is named `mewbo-ide-proxy` and matches
   * that prefix, so a name-based sweep would take the whole feature down on
   * every boot. The label is re-checked per item rather than trusted to the
   * daemon-side filter, because that one mistake is unrecoverable.
   *
   * Cost: O(web-ide containers) — one list call plus one remove per reap. Runs
   * once at boot, never on a request path.
   */
  async sweep(): Promise<SweepSummary> {
    const items = await this.call("list web-ide containers", () =>
      this.docker.listContainers({
        all: true,
        filters: { label: [`${KIND_LABEL}=${KIND_VALUE}`] },
      }),
    );

    let inspected = 0;
    let removed = 0;
    for (const item of items) {
      // Re-check the label rather than trusting the daemon-side filter. The
      // redundancy is deliberate: `mewbo-ide-proxy` matches the name prefix a
      // careless sweep would use, and reaping it takes the whole Web IDE
      // feature down for every session. A cheap second guard against an
      // unrecoverable mistake is worth keeping.
      if (item.Labels?.[KIND_LABEL] !== KIND_VALUE) {
        this.log.warn(
          `ide-broker: sweep skipping ${IdeContainers.describe(item)} — not labelled ${KIND_LABEL}=${KIND_VALUE}`,
        );
        continue;
      }
      inspected += 1;
      const reason = this.sweepReason(item);
      if (reason === null) {
        this.log.info(`ide-broker: sweep keeping ${IdeContainers.describe(item)} — running, deadline ahead`);
        continue;
      }
      try {
        await this.docker.getContainer(item.Id).remove({ force: true });
        removed += 1;
        this.log.info(`ide-broker: sweep removed ${IdeContainers.describe(item)} — ${reason}`);
      } catch (err) {
        const detail = err instanceof Error ? err.message : String(err);
        this.log.warn(
          `ide-broker: sweep could not remove ${IdeContainers.describe(item)}: ${detail}`,
        );
      }
    }
    this.log.info(`ide-broker: sweep complete — inspected ${inspected}, removed ${removed}`);
    return { inspected, removed };
  }

  /** Why this container should be reaped, or null to keep it. */
  private sweepReason(item: DockerListItem): string | null {
    if (item.State !== "running") {
      return `state=${item.State ?? "unknown"}`;
    }
    const sessionId = item.Labels?.[SESSION_LABEL];
    if (sessionId === undefined || sessionId.length === 0) {
      return `missing ${SESSION_LABEL} label`;
    }
    const deadline = this.deadlines.read(sessionId);
    if (deadline === null) {
      return "no deadline file";
    }
    if (deadline.getTime() <= this.now().getTime()) {
      return `deadline passed at ${deadline.toISOString()}`;
    }
    return null;
  }

  private async call<T>(operation: string, fn: () => Promise<T>): Promise<T> {
    try {
      return await fn();
    } catch (err) {
      throw IdeContainers.dockerFailure(operation, err);
    }
  }

  private static describe(item: DockerListItem): string {
    const name = item.Names?.[0]?.replace(/^\//, "");
    return name !== undefined && name.length > 0 ? name : item.Id.slice(0, 12);
  }

  private static isNotFound(err: unknown): boolean {
    return (err as { statusCode?: number }).statusCode === 404;
  }

  /**
   * Classify a docker failure as unreachable (503) or refused (502).
   *
   * The distinction is DIAGNOSTIC, not behavioural — it reaches a log line and
   * stops there, because the Python client maps both onto the same
   * `DockerUnavailable`. Classify on the structured signals only: a
   * `statusCode` means the daemon answered, and an errno in the unreachable
   * set means it did not. Anything else falls to 502, which is the safe
   * reading for a failure whose shape the daemon itself produced.
   */
  private static dockerFailure(operation: string, err: unknown): BrokerError {
    if (err instanceof BrokerError) {
      return err;
    }
    const errno = (err as { code?: string }).code;
    const message = err instanceof Error ? err.message : String(err);
    const hasStatus = typeof (err as { statusCode?: number }).statusCode === "number";
    if (!hasStatus && errno !== undefined && UNREACHABLE_ERRNOS.has(errno)) {
      return BrokerError.dockerUnavailable(`${operation}: ${message}`);
    }
    return BrokerError.dockerError(`${operation}: ${message}`);
  }
}
