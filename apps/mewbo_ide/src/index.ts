import Docker from "dockerode";

import { BrokerConfig } from "./config.js";
import type { DockerClientLike } from "./containers.js";
import { BrokerError } from "./errors.js";
import { BrokerServer } from "./server.js";

try {
  // Fail fast and loudly: an unset token or an empty allowlist must stop the
  // process here, not degrade into a broker that accepts every path.
  const config = BrokerConfig.fromEnv();

  // The single boundary in this service that touches the raw dockerode
  // client. Its surface is far wider than what's used, so it is narrowed to
  // `DockerClientLike` immediately and nothing downstream ever sees it.
  // Three calls pass straight through by shape; `imageExists`/`pullImage`
  // have no same-named dockerode equivalent, so they're built from
  // `getImage`/`pull` here rather than left for `IdeContainers` to know
  // dockerode's calling convention.
  const raw = new Docker({ socketPath: config.dockerSocket });
  const docker: DockerClientLike = {
    listContainers: (options) =>
      raw.listContainers(options) as unknown as ReturnType<DockerClientLike["listContainers"]>,
    getContainer: (id) => raw.getContainer(id) as unknown as ReturnType<DockerClientLike["getContainer"]>,
    createContainer: (spec) =>
      raw.createContainer(
        spec as unknown as Parameters<Docker["createContainer"]>[0],
      ) as unknown as ReturnType<DockerClientLike["createContainer"]>,
    imageExists: async (image) => {
      try {
        await raw.getImage(image).inspect();
        return true;
      } catch (err) {
        if ((err as { statusCode?: number }).statusCode === 404) {
          return false;
        }
        throw err;
      }
    },
    pullImage: (image) =>
      new Promise<void>((resolve, reject) => {
        raw
          .pull(image)
          .then((stream) => {
            raw.modem.followProgress(stream, (err) => {
              if (err) {
                reject(err);
              } else {
                resolve();
              }
            });
          })
          .catch(reject);
      }),
  };

  const server = BrokerServer.create({ config, docker });
  for (const signal of ["SIGINT", "SIGTERM"] as const) {
    process.once(signal, () => {
      void server.close();
    });
  }

  await server.sweep();
  await server.ensureImage();
  await server.listen();
} catch (err) {
  console.error(`ide-broker: refusing to start: ${BrokerError.from(err).reason}`);
  process.exit(1);
}
