# Sandbox

Build the default image used by `DockerSandbox`:

```bash
docker build -f henry/sandbox/Dockerfile.base -t henry-sandbox:base .
```

The V1 runtime starts containers with `network_mode="none"` and a read-only root
filesystem. Repository cloning is performed host-side through the GitHub archive
API, then regular files are copied into the sandbox workspace.
