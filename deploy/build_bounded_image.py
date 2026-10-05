"""Build a Git-source overlay without inheriting the baseline's layer history."""

import json
import re
import subprocess
from pathlib import Path, PurePosixPath

BENCH = "/home/frappe/frappe-bench"
RUNTIME_FIELDS = {
    "Env", "User", "WorkingDir", "Entrypoint", "Cmd", "ExposedPorts",
    "Volumes", "Labels", "StopSignal", "Healthcheck", "Shell",
}
CLEAN_SOURCES = """import json,shutil,sys
from pathlib import Path
root = Path(sys.argv[1]).resolve()
for app in json.loads(sys.argv[2]):
    base = root / app
    if base.resolve().parent != root:
        raise ValueError('App directory escapes source root')
    git = base / '.git'
    if git.is_symlink() or git.is_file():
        git.unlink()
    elif git.is_dir():
        shutil.rmtree(git)
for app,path in json.loads(sys.argv[3]):
    base, target = root / app, root / app / path
    if base.resolve().parent != root or not target.parent.resolve().is_relative_to(base.resolve()):
        raise ValueError('Deleted path escapes app')
    target.unlink(missing_ok=True)
"""


def _quote(value):
    if not isinstance(value, str) or any(ord(char) < 32 for char in value):
        raise ValueError("Dockerfile metadata must be single-line text")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$") + '"'


def _runtime_instructions(config, labels):
    unsupported = [key for key, value in config.items() if value and key not in RUNTIME_FIELDS]
    if unsupported:
        raise ValueError("Unsupported nonempty image config: " + ", ".join(unsupported))
    lines, env_names = [], set()
    for item in config.get("Env") or []:
        name, value = item.split("=", 1)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) or name in env_names:
            raise ValueError("Unsupported or duplicate environment name")
        env_names.add(name)
        lines.append(f"ENV {name}={_quote(value)}")
    for name, value in {**(config.get("Labels") or {}), **labels}.items():
        lines.append(f"LABEL {_quote(name)}={_quote(value)}")
    for key, instruction, pattern in (
        ("User", "USER", r"[A-Za-z0-9_.-]+(?::[A-Za-z0-9_.-]+)?"),
        ("WorkingDir", "WORKDIR", r"/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]*"),
        ("StopSignal", "STOPSIGNAL", r"(?:SIG[A-Z0-9]+|[1-9][0-9]*)"),
    ):
        if config.get(key):
            if not re.fullmatch(pattern, config[key]):
                raise ValueError("Unsupported image config: " + key)
            lines.append(f"{instruction} {config[key]}")
    for key, instruction in (("Shell", "SHELL"), ("Entrypoint", "ENTRYPOINT"), ("Cmd", "CMD")):
        if config.get(key) is not None:
            lines.append(instruction + " " + json.dumps(config[key]))
    for port in config.get("ExposedPorts") or {}:
        if not re.fullmatch(r"[0-9]+/(tcp|udp)", port):
            raise ValueError("Unsupported exposed port")
        lines.append("EXPOSE " + port)
    volumes = config.get("Volumes") or {}
    if volumes:
        if any(not path.startswith("/") or "$" in path or "\\" in path for path in volumes):
            raise ValueError("Unsupported volume path")
        lines.append("VOLUME " + json.dumps(list(volumes)))
    health = config.get("Healthcheck")
    if health:
        options = {"Interval": "interval", "Timeout": "timeout", "StartPeriod": "start-period",
                   "StartInterval": "start-interval", "Retries": "retries"}
        if any(key not in {*options, "Test"} for key in health):
            raise ValueError("Unsupported healthcheck config")
        flags = []
        for key, flag in options.items():
            value = health.get(key, 0)
            if type(value) is not int or value < 0:
                raise ValueError("Invalid healthcheck duration or retries")
            if value:
                flags.append(f"--{flag}={value}{'' if key == 'Retries' else 'ns'}")
        test = health.get("Test", [])
        if test == ["NONE"] and not flags:
            lines.append("HEALTHCHECK NONE")
        elif len(test) > 1 and test[0] == "CMD":
            lines.append("HEALTHCHECK " + " ".join(flags + ["CMD", json.dumps(test[1:])]))
        elif len(test) == 2 and test[0] == "CMD-SHELL" and "\n" not in test[1] and "\r" not in test[1]:
            lines.append("HEALTHCHECK " + " ".join(flags + ["CMD", test[1]]))
        else:
            raise ValueError("Unsupported healthcheck test")
    return lines


def _deleted_paths(manifest):
    deleted = []
    for app, source in manifest.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", app):
            raise ValueError("Invalid app name")
        for path in source.get("deleted", []):
            parts = path.split("/")
            if any(part in ("", ".", "..", "node_modules") for part in parts) or PurePosixPath(path).is_absolute():
                raise ValueError("Unsafe deleted source path")
            deleted.append([app, path])
    return deleted


def _inspect(image):
    images = json.loads(subprocess.check_output(["docker", "image", "inspect", image], text=True))
    if len(images) != 1 or not re.fullmatch(r"sha256:[0-9a-f]{64}", images[0]["Id"]):
        raise ValueError("Expected one immutable Docker image ID")
    return images[0]


def _pin_base(image_id):
    tag = "leya/build-input:" + image_id.removeprefix("sha256:")
    existing = subprocess.check_output(
        ["docker", "image", "ls", "--no-trunc", "--quiet", "--filter", "reference=" + tag], text=True
    ).splitlines()
    if existing and existing != [image_id]:
        raise ValueError("Build input tag already identifies a different image")
    if not existing:
        subprocess.run(["docker", "tag", image_id, tag], check=True)
    if _inspect(tag)["Id"] != image_id:
        raise ValueError("Build input tag does not identify the pinned image")
    return tag


def _verify(base, candidate, labels):
    layers = candidate.get("RootFS", {}).get("Layers", [])
    if not 1 <= len(layers) <= 4:
        raise ValueError("Candidate must have between one and four filesystem layers")
    expected = {**base["Config"], "Labels": {**(base["Config"].get("Labels") or {}), **labels}}
    for key in RUNTIME_FIELDS:
        if expected.get(key) != candidate["Config"].get(key):
            raise ValueError("Candidate runtime config differs: " + key)
    for key in ("Os", "Architecture", "Variant"):
        if base.get(key) != candidate.get(key):
            raise ValueError("Candidate platform differs: " + key)


def build(base_image, candidate_image, context_dir, labels, build_apps=("flow",)):
    """Build and verify a candidate; do not run containers or mount production data."""
    if candidate_image == base_image:
        raise ValueError("Candidate must use a different image reference")
    base = _inspect(base_image)
    context = Path(context_dir).resolve()
    manifest = json.loads((context / "candidate-sources.json").read_text())
    deleted = _deleted_paths(manifest)
    metadata = _runtime_instructions(base["Config"], labels)
    base_tag = _pin_base(base["Id"])
    lines = [f"FROM {base_tag} AS builder", "USER root",
             "RUN " + json.dumps(["python3", "-c", CLEAN_SOURCES, BENCH + "/apps",
                                  json.dumps(list(manifest)), json.dumps(deleted)])]
    lines += [f'COPY --chown=frappe:frappe ["app-source/", "{BENCH}/apps/"]',
              "USER frappe", f"WORKDIR {BENCH}"]
    for app in build_apps:
        if app not in manifest:
            raise ValueError("Build app is missing from candidate source manifest")
        lines.append("RUN " + json.dumps(["bench", "build", "--app", app, "--production", "--force"]))
    lines += ["FROM scratch", 'COPY --from=builder ["/", "/"]', *metadata]
    dockerfile = context / "Dockerfile.bounded"
    dockerfile.write_text("\n".join(lines) + "\n")
    (context / "Dockerfile.bounded.dockerignore").write_text("**\n!app-source/\n!app-source/**\n")
    for reference in (base_image, base_tag):
        if _inspect(reference)["Id"] != base["Id"]:
            raise ValueError("Baseline image reference changed before build")
    subprocess.run(["docker", "build", "--pull=false", "--file", str(dockerfile),
                    "--tag", candidate_image, str(context)], check=True)
    for reference in (base_image, base_tag):
        if _inspect(reference)["Id"] != base["Id"]:
            raise ValueError("Baseline image reference changed during build")
    candidate = _inspect(candidate_image)
    _verify(base, candidate, labels)
    return candidate
