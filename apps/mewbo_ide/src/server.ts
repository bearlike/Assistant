import Fastify from "fastify";
import type { FastifyInstance, FastifyServerOptions } from "fastify";

import { WorkspaceAllowlist } from "./allowlist.js";
import type { BrokerConfig } from "./config.js";
import type { BrokerLogger, DockerClientLike, SweepSummary } from "./containers.js";
import { IdeContainers } from "./containers.js";
import { DeadlineFiles } from "./deadlines.js";
import { DefaultSettingsFile } from "./default_settings.js";
import { BrokerError } from "./errors.js";
import { IdeRoutes } from "./routes.js";

export interface BrokerServerOptions {
  config: BrokerConfig;
  /** The one collaborator that varies between production and a test. */
  docker: DockerClientLike;
  now?: () => Date;
  logger?: FastifyServerOptions["logger"];
}

/**
 * Composition root and process lifecycle.
 *
 * Everything below it is injected: `create` builds the deadline store, the
 * allowlist and the container controller from one `BrokerConfig` plus one
 * docker client, so a test swaps the daemon by passing a fake and changes
 * nothing else.
 */
export class BrokerServer {
  readonly instance: FastifyInstance;
  private readonly config: BrokerConfig;
  private readonly containers: IdeContainers;
  private readonly log: BrokerLogger;

  private constructor(
    config: BrokerConfig,
    instance: FastifyInstance,
    containers: IdeContainers,
    log: BrokerLogger,
  ) {
    this.config = config;
    this.instance = instance;
    this.containers = containers;
    this.log = log;
  }

  static create(options: BrokerServerOptions): BrokerServer {
    const { config, docker } = options;
    const now = options.now ?? ((): Date => new Date());
    const instance = Fastify({ logger: options.logger ?? true });

    const log: BrokerLogger = {
      info: (message: string): void => instance.log.info(message),
      warn: (message: string): void => instance.log.warn(message),
    };

    const deadlines = new DeadlineFiles(config.stateDir);
    const settings = new DefaultSettingsFile(config.stateDir);
    // A configured volume root is trusted by construction, not by a separate
    // operator action: it is already broker config (`MEWBO_IDE_VOLUME_ROOTS`),
    // the same trust tier as `MEWBO_IDE_ALLOWED_ROOTS`, so it is unioned in
    // rather than requiring a second, redundant allowlist entry. This does not
    // weaken containment — `WorkspaceAllowlist` still `realpath`s every root
    // and every candidate, so a volume root the broker cannot see (because the
    // matching named volume isn't ALSO mounted into this container at the same
    // path) still resolves to nothing and 403s, exactly like a misconfigured
    // bind root does today.
    const allowlist = new WorkspaceAllowlist([
      ...config.allowedRoots,
      ...config.volumeRoots.map((volumeRoot) => volumeRoot.root),
    ]);
    const containers = new IdeContainers(docker, config, deadlines, settings, log, now);

    // One rendering seam for every refusal, so a typed error thrown deep in the
    // allowlist or the container controller reaches the wire unchanged.
    instance.setErrorHandler((err, _request, reply) => {
      const failure = BrokerError.from(err);
      // A 502/503 is a CLASSIFIED refusal — the daemon is down or said no — so
      // it logs as a warning. Only `internal_error` means the broker itself
      // failed, and only that deserves an error line an operator should chase.
      if (failure.code === "internal_error") {
        instance.log.error(`ide-broker: unhandled failure: ${failure.reason}`);
      } else if (failure.status >= 500) {
        instance.log.warn(`ide-broker: ${failure.code}: ${failure.reason}`);
      }
      void reply.code(failure.status).type("application/json").send(failure.body());
    });
    instance.setNotFoundHandler((request, reply) => {
      const failure = BrokerError.notFound(`no route for ${request.method} ${request.url}`);
      void reply.code(failure.status).type("application/json").send(failure.body());
    });

    new IdeRoutes(config, allowlist, containers, deadlines, now).register(instance);

    return new BrokerServer(config, instance, containers, log);
  }

  /**
   * Reap stranded containers. Never throws: a daemon that is not up yet must
   * delay the sweep, not the listener — a broker that refuses to serve
   * `/healthz` because it could not tidy up is harder to diagnose than one that
   * logs the failure and answers.
   */
  async sweep(): Promise<SweepSummary | null> {
    try {
      return await this.containers.sweep();
    } catch (err) {
      const reason = err instanceof Error ? err.message : String(err);
      this.log.warn(`ide-broker: startup sweep failed, continuing: ${reason}`);
      return null;
    }
  }

  /**
   * Pull the configured image before serving, so the FIRST launch on a fresh
   * host doesn't pay a cold registry pull inline with a session open — and so
   * a host that never had the image at all doesn't fail every launch forever.
   * Never throws, for the same reason `sweep` doesn't: a registry hiccup at
   * boot must delay readiness, not refuse it — `create`'s own `ensureImage`
   * call retries on the next launch either way.
   */
  async ensureImage(): Promise<void> {
    try {
      await this.containers.ensureImage();
    } catch (err) {
      const reason = err instanceof Error ? err.message : String(err);
      this.log.warn(`ide-broker: startup image pull failed, continuing: ${reason}`);
    }
  }

  async listen(): Promise<string> {
    const address = await this.instance.listen({
      host: this.config.host,
      port: this.config.port,
    });
    this.log.info(
      `ide-broker: listening on ${address}; image=${this.config.image} ` +
        `network=${this.config.network} roots=${this.config.allowedRoots.join(", ")}`,
    );
    return address;
  }

  async close(): Promise<void> {
    await this.instance.close();
  }
}
