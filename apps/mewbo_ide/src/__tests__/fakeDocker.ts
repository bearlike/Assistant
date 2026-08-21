import type {
  ContainerSpec,
  DockerClientLike,
  DockerContainerHandle,
  DockerListItem,
  DockerListOptions,
} from "../containers.js";

/** Mirrors dockerode's own 404 shape, which the production code narrows on. */
export class DockerNotFound extends Error {
  readonly statusCode = 404;

  constructor(id: string) {
    super(`(HTTP code 404) no such container - No such container: ${id}`);
    this.name = "DockerNotFound";
  }
}

/** Mirrors a transport failure: no `statusCode`, an errno instead. */
export class DockerUnreachable extends Error {
  readonly code = "ENOENT";

  constructor() {
    super("connect ENOENT /var/run/docker.sock");
    this.name = "DockerUnreachable";
  }
}

export interface FakeContainer {
  Id: string;
  Names: string[];
  State: string;
  Labels: Record<string, string>;
}

/**
 * A daemon-shaped double.
 *
 * Records every call so a test can assert what the broker asked for, not only
 * what it returned — the sweep's label filter is only provable that way.
 */
export class FakeDockerClient implements DockerClientLike {
  readonly created: ContainerSpec[] = [];
  readonly started: string[] = [];
  readonly removed: string[] = [];
  readonly listCalls: DockerListOptions[] = [];
  containers: FakeContainer[] = [];

  /**
   * When false, `listContainers` ignores the label filter entirely — which is
   * how a test proves the sweep's OWN label re-check protects `mewbo-ide-proxy`
   * rather than relying on the daemon-side filter.
   */
  honorFilters = true;

  /** Set to make the next `createContainer` throw. */
  createFailure: Error | null = null;

  /** Whether `imageExists` reports the configured image already present. */
  imagePresent = true;

  /** Set to make the next `pullImage` throw instead of succeeding. */
  pullFailure: Error | null = null;

  /** Every image name `pullImage` was asked to fetch, in call order. */
  readonly pulled: string[] = [];

  async imageExists(): Promise<boolean> {
    return this.imagePresent;
  }

  async pullImage(image: string): Promise<void> {
    this.pulled.push(image);
    if (this.pullFailure !== null) {
      throw this.pullFailure;
    }
    this.imagePresent = true;
  }

  async listContainers(options: DockerListOptions): Promise<DockerListItem[]> {
    this.listCalls.push(options);
    const labels = options.filters?.label ?? [];
    const matched =
      this.honorFilters && labels.length > 0
        ? this.containers.filter((container) =>
            labels.every((pair) => FakeDockerClient.hasLabel(container, pair)),
          )
        : this.containers;
    return matched.map((container) => ({
      Id: container.Id,
      Names: [...container.Names],
      State: container.State,
      Labels: { ...container.Labels },
    }));
  }

  getContainer(id: string): DockerContainerHandle {
    return {
      start: async (): Promise<void> => {
        const found = this.require(id);
        found.State = "running";
        this.started.push(found.Id);
      },
      remove: async (): Promise<void> => {
        const found = this.require(id);
        this.containers = this.containers.filter((container) => container.Id !== found.Id);
        this.removed.push(FakeDockerClient.displayName(found));
      },
      inspect: async (): Promise<{ State?: { Status?: string } }> => {
        const found = this.require(id);
        return { State: { Status: found.State } };
      },
    };
  }

  async createContainer(spec: ContainerSpec): Promise<DockerContainerHandle> {
    if (this.createFailure !== null) {
      throw this.createFailure;
    }
    this.created.push(spec);
    const container: FakeContainer = {
      Id: `id-${spec.name}`,
      Names: [`/${spec.name}`],
      State: "created",
      Labels: { ...spec.Labels },
    };
    this.containers.push(container);
    return this.getContainer(container.Id);
  }

  /** Seed a container as if it were already on the host. */
  seed(container: FakeContainer): void {
    this.containers.push(container);
  }

  private require(id: string): FakeContainer {
    const found = this.containers.find(
      (container) =>
        container.Id === id ||
        container.Names.includes(id) ||
        container.Names.includes(`/${id}`),
    );
    if (found === undefined) {
      throw new DockerNotFound(id);
    }
    return found;
  }

  private static displayName(container: FakeContainer): string {
    return container.Names[0]?.replace(/^\//, "") ?? container.Id;
  }

  private static hasLabel(container: FakeContainer, pair: string): boolean {
    const separator = pair.indexOf("=");
    if (separator === -1) {
      return pair in container.Labels;
    }
    const key = pair.slice(0, separator);
    const value = pair.slice(separator + 1);
    return container.Labels[key] === value;
  }
}
