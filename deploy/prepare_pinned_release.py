#!/usr/bin/env python3
"""Prepare a bounded ERP stack image; reuse the verified release/rollback runner.

The Shipping migration runner is retained as deployment tooling, not as the
owner of ERP/Flow behavior. This preparation belongs to ERPNext deployment and
adds complete Git source checks for the framework, CRM and Insights. Production
switching still requires the runner's isolated rehearsal and backup gates.
"""

import importlib.util
import json
from pathlib import Path
import re
import shutil
import sys
import tarfile


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def pin_baseline_build_outputs(sources, image_id):
    """Identify the inspected legacy CRM build without weakening Git checks.

    Its nested, unfrozen postinstall upgraded cropperjs dependencies, and Vite
    reformatted auto-imports.d.ts. Only this immutable baseline may have these
    bytes; candidate source remains the unmodified Git archive.
    """
    if image_id != "sha256:1cbd9e1faf265ea7266320a2f71387953ef2df2628f30abf0808df4c9b2796ef":
        return
    crm = sources["crm"]
    assert crm["sha"] == "a82db7522f416433aa792fbe572116e23e62abc2"
    outputs = {
        "frontend/auto-imports.d.ts": ("7099468b538479c823294707e289600fcb7dd9c5da0faa8c07b38a6afac0d76f", "194ee3276321bf899a3206b1ff852cf99cc86412bfc90ac9abbd6ad7911e2dac"),
        "frontend/yarn.lock": ("c5a0d03b6a2f74fa092e07646a2ead0bd6f4c804d2868647be40e44b7911f602", "196592c530cd061a21df6c97a507fa18a67cb32bebf20c2543766479d4fceb83"),
    }
    crm["build_outputs"] = {}
    for path, (git_hash, build_hash) in outputs.items():
        assert crm["files"][path] == git_hash, "unexpected_baseline_git_file:" + path
        crm["build_outputs"][path] = {"git_sha256": git_hash, "image_sha256": build_hash}
        crm["files"][path] = build_hash


def verify_pins(root):
    """Recheck all seven Git pins and candidate Git identities before switching."""
    root = Path(root).resolve()
    release = load_module("pinned_release", root / "release.py")
    saved = release.load(root / "pinned-build.json")
    release.state()
    for name, digest in saved["build_tools"].items():
        assert release.sha(root / name) == digest, "build_tool_changed:" + name
    for app, source in saved["sources"].items():
        actual = release.run(["git", "ls-remote", source["remote"], "refs/heads/" + source["branch"]], capture=True)
        assert actual.split()[0] == source["revision"], "remote_head_changed:" + app
    expected = saved["revisions"]
    probe = "import json,subprocess,sys; refs=json.loads(sys.argv[1]); root='/home/frappe/frappe-bench/apps/'; actual={app:subprocess.check_output(['git','-C',root+app,'rev-parse','HEAD'],text=True).strip() for app in refs}; assert actual==refs,(actual,refs); print('SEVEN_APP_GIT_IDENTITIES_OK')"
    release.run(["docker", "run", "--rm", "--network", "none", "--entrypoint", release.PYTHON, release.required("NEW_IMAGE"), "-c", probe, json.dumps(expected)])
    image_labels = release.inspection(release.required("NEW_IMAGE"))["Config"]["Labels"]
    for app, revision in expected.items():
        label = "org.leya." + {"erpnext_shipping": "shipping", "sf_international": "sf"}.get(app, app) + "-revision"
        assert image_labels.get(label) == revision
    print("ALL_RELEASE_PINS_VERIFIED", flush=True)


def prepare(root):
    root = Path(root).resolve()
    release = load_module("pinned_release", root / "release.py")
    verifier = load_module("pinned_verifier", root / "verify_code.py")
    builder = load_module("bounded_builder", root / "build_bounded_image.py")
    assert release.metadata_script_relative() == "app-source/flow/deploy/static_assets/metadata.py", "this_source_only_release_requires_read_only_metadata"
    assert not (root / "release-state.json").exists(), "use_new_release_directory"
    assert not (root / "git-source").exists(), "use_new_release_directory"
    refs = release.source_refs()
    for label, app in (("FRAPPE", "frappe"), ("CRM", "crm"), ("INSIGHTS", "insights")):
        remote, branch, revision, baseline = (
            release.required(label + suffix)
            for suffix in ("_REMOTE", "_BRANCH", "_REV", "_BASE_REV")
        )
        assert remote.startswith("git@github.com:XASDSdsa/"), "wrong_ssh_remote"
        assert re.fullmatch(r"[0-9a-f]{40}", revision)
        actual = release.run(["git", "ls-remote", remote, "refs/heads/" + branch], capture=True)
        assert actual.split()[0] == revision, "remote_head_changed:" + label
        refs.append((app, app, remote, revision, baseline))

    base, candidate = release.required("BASE_IMAGE"), release.required("NEW_IMAGE")
    base_id = release.image_id(base)
    assert base != candidate
    release.running(base, base_id)
    assert release.run(["docker", "image", "inspect", candidate], capture=True, check=False).returncode != 0
    configuration = json.loads(release.run(release.compose() + ["config", "--format", "json"], capture=True))
    release.prepare_override(Path(release.required("PROJECT_PATH")) / "build/zh-cn/compose.zh-cn.yaml", base, candidate, configuration)

    (root / "git-source").mkdir()
    baseline_sources, candidate_sources, changed = {}, {}, {}
    for checkout, app, remote, revision, baseline in refs:
        repo = root / "git-source" / checkout
        release.run(["git", "init", "-q", repo])
        release.run(["git", "-C", repo, "remote", "add", "origin", remote])
        release.run(["git", "-C", repo, "fetch", "--depth=1", "origin", baseline, revision])
        release.run(["git", "-C", repo, "checkout", "--detach", revision])
        paths = release.run(["git", "-C", repo, "diff", "--name-only", baseline, revision], capture=True).splitlines()
        assert not any(Path(p).name in {"pyproject.toml", "package.json", "yarn.lock", "requirements.txt", "uv.lock", "package-lock.json"} for p in paths), "dependency_change_requires_full_dependency_build:" + app
        changed[app] = paths
        baseline_sources[app] = verifier.archive_manifest(repo, baseline)
        candidate_sources[app] = verifier.archive_manifest(repo, revision)
        candidate_sources[app]["deleted"] = sorted(set(baseline_sources[app]["files"]) - set(candidate_sources[app]["files"]))
        archive = root / (checkout + ".tar")
        release.run(["git", "-C", repo, "archive", "--output", archive, revision])
        destination = root / "app-source" / app
        destination.mkdir(parents=True)
        with tarfile.open(archive) as stream:
            stream.extractall(destination, filter="data")
        # Preserve the exact detached checkout identity alongside its Git archive.
        # This contains only the newly fetched SSH remote and commit objects.
        shutil.copytree(repo / ".git", destination / ".git")

    for filename in release.TOOLS:
        assert (root / "git-source/shipping/deploy/sf_provider_migration" / filename).read_bytes() == (root / filename).read_bytes(), "release_tool_not_from_git:" + filename
    for filename in ("prepare_pinned_release.py", "build_bounded_image.py"):
        assert (root / "git-source/erpnext/deploy" / filename).read_bytes() == (root / filename).read_bytes(), "build_tool_not_from_git:" + filename
    pin_baseline_build_outputs(baseline_sources, base_id)
    release.save("changed-paths.json", changed)
    release.save("baseline-sources.json", baseline_sources)
    release.save("candidate-sources.json", candidate_sources)
    release.verify(base, "baseline-sources.json", image=True, assets_out="baseline-assets.json")
    for service in ("backend", "frontend"):
        release.verify(release.container_name(service), "baseline-sources.json", assets_match="baseline-assets.json")
    release.prepare_metadata_permissions()

    labels = {"org.leya." + {"erpnext_shipping": "shipping", "sf_international": "sf"}.get(app, app) + "-revision": revision for _, app, _, revision, _ in refs}
    labels["com.leya.release"] = release.required("RELEASE_NAME")
    builder.build(base, candidate, root, labels, build_apps=["flow"])
    release.verify(candidate, "candidate-sources.json", image=True, baseline_assets="baseline-assets.json", assets_out="candidate-assets.json")
    assert release.image_id(base) == base_id, "baseline_image_changed"
    release.save("pinned-build.json", {
        "revisions": {app: revision for _, app, _, revision, _ in refs},
        "sources": {app: {"remote": remote, "revision": revision, "branch": release.required({"erpnext": "ERP", "sf_international": "SF", "erpnext_shipping": "SHIPPING"}.get(app, app.upper()) + "_BRANCH")} for _, app, remote, revision, _ in refs},
        "build_tools": {name: release.sha(root / name) for name in ("prepare_pinned_release.py", "build_bounded_image.py")},
        "image_layers": len(release.inspection(candidate)["RootFS"]["Layers"]),
    })
    release.save("release-state.json", {
        "scripts": release.scripts(), "base_id": base_id,
        "candidate_id": release.image_id(candidate),
        "revisions": {app: revision for _, app, _, revision, _ in refs[:4]},
    })
    verify_pins(root)
    print("CANDIDATE_IMAGE_READY; run isolated rehearsal before deploy", flush=True)


if __name__ == "__main__":
    assert len(sys.argv) in (2, 3), "usage: prepare_pinned_release.py RELEASE_DIRECTORY [verify]"
    if len(sys.argv) == 3:
        assert sys.argv[2] == "verify"
        verify_pins(sys.argv[1])
    else:
        prepare(sys.argv[1])
