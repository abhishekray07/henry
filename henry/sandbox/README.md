# Sandbox

Henry's `run_python` tool executes cells in a stateful IPython kernel inside a
run-scoped Docker container. Variables, imports, and definitions persist for
the task. The host reaches the kernel with `docker exec`; the container keeps
`network_mode="none"`, a read-only root filesystem, dropped capabilities, and
a writable `/workspace` volume.

Build the default image with `henry/sandbox` as the build context:

```bash
docker build -f henry/sandbox/Dockerfile.base -t henry-sandbox:base henry/sandbox
```

Each execution returns a `CellResult` whose `outputs` list preserves Jupyter
event order. Stream, execute-result, display-data, and error outputs retain
their nbformat-shaped fields, including complete MIME bundles and metadata.
Output is bounded by serialized UTF-8 byte, per-item, field, and item-count
limits before the host parses it.

Repository cloning remains host-side through the GitHub archive API; only safe
regular files are copied into the isolated workspace. Run the live smoke suite
after building the image:

```bash
uv run pytest -m integration -q
```

Display updates are not replayed yet: `update_display_data` is flattened into a
new `display_data` output, and `clear_output` is ignored. Hosted notebook work
will add display-ID replacement and clearing semantics.

`requirements-kernel.txt` pins the two direct kernel dependencies only. The
base image, apt packages, and transitive Python dependencies still float; a
digest-pinned image and complete constraints file are separate follow-up work.
