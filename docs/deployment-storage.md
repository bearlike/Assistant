# Storage Backends

Truss stores session transcripts, compaction summaries, titles, and metadata in a pluggable storage backend. The default is a JSON filesystem store. It has zero dependencies and works immediately after installation. Switch to MongoDB for multi-instance deployments, persistence across container restarts, or when using the [Web IDE](features-web-ide.md) feature (which requires MongoDB for container state).

## JSON (Default)

The JSON driver writes one file per session under `$TRUSS_HOME/sessions/` (default: `~/.truss/sessions/`). No extra dependencies are needed.

```bash
# These two are equivalent. JSON is the default
TRUSS_STORAGE_DRIVER=json
# or simply leave it unset
```

**Suitable for:** single-instance use, local development, CLI sessions.

**Not suitable for:** running multiple API workers simultaneously (no cross-process locking), or when you need the Web IDE feature.

## MongoDB

```bash
TRUSS_STORAGE_DRIVER=mongodb
TRUSS_MONGODB_URI=mongodb://truss:truss@localhost:27018/truss?authSource=admin
TRUSS_MONGODB_DATABASE=truss
```

The MongoDB driver stores all session data in collections within the configured database. Connection settings are read from environment variables, which override anything set in `configs/app.json`.

> [!IMPORTANT] Required for the Web IDE
> The [Web IDE](features-web-ide.md) feature needs MongoDB to persist container state across API restarts. Without MongoDB, the Web IDE button stays disabled.

> [!TIP] Recommended for production
> Docker deployments, multi-worker API setups, and any environment where session data must survive container restarts should prefer MongoDB over the JSON driver.

### Adding MongoDB to the Docker Compose Stack

MongoDB is already defined as a service in `docker-compose.yml`. To activate it, add the following to your `docker-compose.override.yml` (or `docker.env`):

```dotenv
# docker.env
TRUSS_STORAGE_DRIVER=mongodb
TRUSS_MONGODB_URI=mongodb://truss:truss@127.0.0.1:27018/truss?authSource=admin
TRUSS_MONGODB_DATABASE=truss
```

The MongoDB service uses host networking (`ports: ["${MONGO_PORT:-27018}:27017"]`), so `127.0.0.1:27018` is reachable from the API container (also on host network).

**Example `docker-compose.override.yml` snippet for a self-hosted MongoDB:**

```yaml
services:
  api:
    environment:
      - TRUSS_STORAGE_DRIVER=mongodb
      - TRUSS_MONGODB_URI=mongodb://truss:truss@127.0.0.1:27018/truss?authSource=admin
```

### External MongoDB

If you are using Atlas or another hosted MongoDB service, set `TRUSS_MONGODB_URI` to your connection string and ensure the URI includes the `authSource` parameter if required:

```dotenv
TRUSS_MONGODB_URI=mongodb+srv://user:pass@cluster.mongodb.net/truss
TRUSS_MONGODB_DATABASE=truss
```

## Switching Between Drivers

There is **no automatic migration** between storage drivers. If you switch from JSON to MongoDB (or vice versa), existing sessions in the old store are not accessible from the new driver.

To preserve history before switching:
1. Export sessions you want to keep via `GET /api/sessions/{id}/export`.
2. Change `TRUSS_STORAGE_DRIVER` and restart.

## Configuration Reference

| Variable / Config key | Source | Default | Description |
|----------------------|--------|---------|-------------|
| `TRUSS_STORAGE_DRIVER` | Env var | `json` | Storage driver: `json` or `mongodb`. Env var takes precedence over `configs/app.json`. |
| `TRUSS_MONGODB_URI` | Env var | `mongodb://localhost:27017` | Full MongoDB connection URI. |
| `TRUSS_MONGODB_DATABASE` | Env var | `truss` | MongoDB database name. |
| `TRUSS_HOME` | Env var | `~/.truss` | Data root for the JSON driver. In Docker, set to `/app/data` (mapped to the `api-data` named volume). |
| `storage.driver` | `configs/app.json` | `json` | Config file equivalent of `TRUSS_STORAGE_DRIVER` (env var wins). |
| `storage.mongodb.uri` | `configs/app.json` | `mongodb://localhost:27017` | Config file equivalent of `TRUSS_MONGODB_URI` (env var wins). |
| `storage.mongodb.database` | `configs/app.json` | `truss` | Config file equivalent of `TRUSS_MONGODB_DATABASE` (env var wins). |

Environment variables always take precedence over values in `configs/app.json`.
