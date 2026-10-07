#!/usr/bin/env python3
"""Build-only helpers for a fresh Git-sourced Frappe image."""
import hashlib
import json
import os
import shlex
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BENCH = "/home/frappe/frappe-bench"
SITES = BENCH + "/sites"
PYTHON = BENCH + "/env/bin/python"
SERVICES = ["backend", "websocket", "frontend", "queue-long", "queue-short", "scheduler"]
TOOLS = ("release.py", "verify_code.py", "prepare.py", "Containerfile", "README.md", "assets-entrypoint.sh")
os.umask(0o077)
os.chdir(ROOT)


def required(key):
    value = os.environ.get(key)
    assert value, "missing_environment:" + key
    return value


def run(arguments, *, capture=False, input=None, check=True, timeout=None):
    print("+ " + shlex.join([str(arg) for arg in arguments]), flush=True)
    result = subprocess.run([str(arg) for arg in arguments], input=input,
                            stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.STDOUT if capture else None,
                            check=False, timeout=timeout)
    if check and result.returncode:
        if capture:
            print(result.stdout.decode(errors="replace"))
        raise RuntimeError("command_failed:" + str(arguments[0]) + ":" + str(result.returncode))
    return result.stdout.decode().strip() if capture and check else result


def save(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def load(path):
    return json.loads(Path(path).read_text())


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def scripts():
    return {name: sha(ROOT / name) for name in TOOLS}


def inspection(target):
    return json.loads(run(["docker", "inspect", target], capture=True))[0]


def image_id(image):
    return run(["docker", "image", "inspect", image, "--format", "{{.Id}}"], capture=True)


def compose(override=None):
    project = Path(required("PROJECT_PATH"))
    return ["docker", "compose", "--env-file", project / required("PRODUCTION_ENV_FILE"),
            "-f", project / required("PRODUCTION_COMPOSE_FILE"), "-f",
            override or project / required("CANDIDATE_COMPOSE_FILE"), "-p", required("PROJECT")]


def running(expected_image, expected_id, services=SERVICES):
    for service in services:
        actual = inspection(required("PROJECT") + "-" + service + "-1")
        assert actual["State"]["Running"] and actual["Config"]["Image"] == expected_image and actual["Image"] == expected_id, "service_version:" + service


def verify(target, manifest, image=False, **options):
    command = ["python3", "verify_code.py", "image" if image else "container", target, manifest]
    for key, value in options.items():
        command.extend(["--" + key.replace("_", "-"), value])
    run(command)


def verify_startup(target, *, image=False):
    config = inspection(target)["Config"]
    assert config["Entrypoint"] == ["/usr/local/bin/entrypoint.sh"], "unexpected_asset_entrypoint"
    command = ["docker", "run", "--rm", "--network", "none", "--entrypoint", PYTHON, target] if image else ["docker", "exec", target, PYTHON]
    code = "import pathlib; p=pathlib.Path('/usr/local/bin/entrypoint.sh'); assert p.stat().st_mode & 0o777 == 0o755; print('STARTUP_SOURCE_OK')"
    assert "STARTUP_SOURCE_OK" in run(command + ["-c", code], capture=True)


def prepare_override(path, old, new, configuration):
    parser = "import json,sys,yaml; print(json.dumps(yaml.safe_load(sys.stdin.read()) or {}))"
    content = json.loads(run(["docker", "run", "--rm", "-i", "--network", "none", "--entrypoint", PYTHON, old, "-c", parser], capture=True, input=path.read_bytes()))
    assert isinstance(content, dict) and isinstance(content.setdefault("services", {}), dict), "override_shape_changed"
    services = content["services"]
    for service in SERVICES:
        assert configuration["services"][service]["image"] == old, "resolved_baseline_image_changed:" + service
        services.setdefault(service, {}).update(image=new, init=True)
    target = ROOT / "compose.override.candidate.json"
    save(target, content)
    planned = json.loads(run(compose(target) + ["config", "--format", "json"], capture=True))
    expected = json.loads(json.dumps(configuration))
    for service in SERVICES:
        expected["services"][service].update(image=new, init=True)
    assert planned == expected, "candidate_compose_changes_outside_images_and_init"
    return target
