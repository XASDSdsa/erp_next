#!/usr/bin/env python3
"""Prepare one clean Git release using the pinned Shipping release operations."""
import argparse
import importlib.util
import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APPS = (
    ("FRAPPE", "frappe", "FRAPPE_BASE_REV", "frappe"),
    ("PAYMENTS", "payments", "PAYMENTS_BASE_REV", "payments"),
    ("ERP", "erpnext", "BASE_ERPNEXT_REV", "erpnext"),
    ("SHIPPING", "erpnext_shipping", "BASE_SHIPPING_REV", "shipping"),
    ("SF", "sf_international", "BASE_SF_REV", "sf"),
    ("CRM", "crm", "CRM_BASE_REV", "crm"),
    ("INSIGHTS", "insights", "INSIGHTS_BASE_REV", "insights"),
    ("FLOW", "flow", "BASE_FLOW_REV", "flow"),
)
CLEAN_TOOLS = ("prepare.py", "Containerfile", "README.md")
PREPARED_FILES = ("baseline-git-sources.json", "baseline-sources.json", "candidate-sources.json",
                  "changed-paths.json", "apps.json", "source-remotes.json", "Containerfile.dockerignore", "baseline-assets.json")
GIT_CHECK = """import json,subprocess,sys
expected=json.load(sys.stdin)
checked={}
for app,source in expected.items():
    command=['git','-C','/home/frappe/frappe-bench/apps/'+app]
    head=subprocess.check_output(command+['rev-parse','HEAD'],text=True).strip()
    assert head==source['revision'], ('git_head_mismatch',app)
    names=subprocess.check_output(command+['remote'],text=True).splitlines()
    assert names, ('missing_git_remote',app)
    remotes={name:subprocess.check_output(command+['remote','get-url',name],text=True).strip() for name in names}
    assert all(url==source['remote'] for url in remotes.values()), ('git_remote_mismatch',app)
    checked[app]={'head':head,'remotes':remotes}
print(json.dumps(checked,sort_keys=True))
"""


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / (name + ".py"))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def sources(r, verifier, inputs, inputs_path):
    assert not Path("release-state.json").exists(), "release_already_finalized"
    base, candidate = r.required("BASE_IMAGE"), r.required("NEW_IMAGE")
    base_id = r.image_id(base)
    assert base_id == r.required("BASE_IMAGE_ID") and base != candidate, "baseline_identity_mismatch"
    r.running(base, base_id)
    assert r.run(["docker", "image", "inspect", candidate], capture=True, check=False).returncode != 0, "candidate_tag_already_exists"
    configuration = json.loads(r.run(r.compose() + ["config", "--format", "json"], capture=True))
    r.prepare_override(Path(r.required("PROJECT_PATH")) / "build/zh-cn/compose.zh-cn.yaml", base, candidate, configuration)
    for directory in ("source-repos", "git-source", "app-source"):
        Path(directory).mkdir(exist_ok=True)
    Path("source-repos").chmod(0o711)
    baseline, target, changed, records = {}, {}, {}, {}
    for prefix, app, baseline_key, alias in APPS:
        remote, branch = r.required(prefix + "_REMOTE"), r.required(prefix + "_BRANCH")
        revision, old = r.required(prefix + "_REV"), r.required(baseline_key)
        user_remote = re.fullmatch(r"git@github\.com:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.git", remote)
        official_payments = app == "payments" and remote == "https://github.com/frappe/payments.git"
        assert user_remote or official_payments, "approved_github_remote_required:" + app
        assert all(re.fullmatch(r"[0-9a-f]{40}", value) for value in (revision, old)), "invalid_revision:" + app
        head = r.run(["git", "ls-remote", remote, "refs/heads/" + branch], capture=True).split()
        assert head and head[0] == revision, "remote_head_changed:" + app
        repo = Path("source-repos") / app
        if not repo.exists():
            r.run(["git", "init", repo])
            r.run(["git", "-C", repo, "remote", "add", "origin", remote])
            r.run(["git", "-C", repo, "fetch", "--depth=1", "origin", old, revision])
            r.run(["git", "-C", repo, "checkout", "-b", "release-source", revision])
        assert r.run(["git", "-C", repo, "remote", "get-url", "origin"], capture=True) == remote
        assert r.run(["git", "-C", repo, "rev-parse", "HEAD"], capture=True) == revision
        assert r.run(["git", "-C", repo, "branch", "--show-current"], capture=True) == "release-source"
        assert not r.run(["git", "-C", repo, "status", "--porcelain", "--untracked-files=all"], capture=True), "dirty_source:" + app
        link = Path("git-source") / alias
        if not link.is_symlink():
            link.symlink_to(Path("../source-repos") / app, target_is_directory=True)
        assert link.resolve() == repo.resolve(), "wrong_source_alias:" + app
        baseline[app] = verifier.archive_manifest(repo, old)
        target[app] = verifier.archive_manifest(repo, revision)
        target[app]["deleted"] = sorted(set(baseline[app]["files"]) - set(target[app]["files"]))
        changed[app] = r.run(["git", "-C", repo, "diff", "--name-only", old, revision], capture=True).splitlines()
        records[app] = {"remote": remote, "branch": branch, "revision": revision, "baseline": old, "label": "org.leya." + alias + "-revision"}
    flow = Path("app-source/flow")
    if not flow.is_symlink():
        flow.symlink_to("../source-repos/flow", target_is_directory=True)
    assert flow.resolve() == Path("source-repos/flow").resolve()
    for name in r.TOOLS:
        assert (Path("source-repos/erpnext_shipping/deploy/sf_provider_migration") / name).read_bytes() == (ROOT / name).read_bytes(), "runner_not_from_target_git:" + name
    for name in CLEAN_TOOLS:
        assert (Path("source-repos/erpnext/deploy/clean_git_release") / name).read_bytes() == (ROOT / name).read_bytes(), "clean_tool_not_from_target_git:" + name
    r.save("baseline-git-sources.json", baseline)
    allowed_by_app = {}
    for prefix, app in (("CRM", "crm"), ("PAYMENTS", "payments")):
        allowed = inputs.get(prefix + "_BASE_BUILD_OUTPUTS", {})
        allowed_by_app[app] = allowed
        if allowed:
            assert inputs[prefix + "_BASE_BUILD_REV"] == records[app]["baseline"], prefix.lower() + "_build_revision_mismatch"
            for path, hashes in allowed.items():
                assert baseline[app]["files"][path] == hashes["git_sha256"], prefix.lower() + "_original_git_hash_mismatch:" + path
                assert re.fullmatch(r"[0-9a-f]{64}", hashes["built_sha256"]), "invalid_" + prefix.lower() + "_build_hash"
                baseline[app]["files"][path] = hashes["built_sha256"]
    r.save("baseline-sources.json", baseline)
    r.save("candidate-sources.json", target)
    r.save("changed-paths.json", changed)
    # ``sf_international`` is retained only as a migration source.  The
    # runtime carrier implementation is owned by ``erpnext_shipping`` and
    # must be the only SF app installed in the candidate bench.
    r.save("apps.json", [{"url": "file:///opt/git/" + app, "branch": "release-source"}
                          for _, app, _, _ in APPS if app not in {"frappe", "sf_international"}])
    r.save("source-remotes.json", {app: source["remote"] for app, source in records.items()})
    Path("Containerfile.dockerignore").write_text("**\n!source-repos/\n!source-repos/**\n!apps.json\n!source-remotes.json\n")
    r.verify(base, "baseline-sources.json", image=True, assets_out="baseline-assets.json")
    for service in ("backend", "frontend"):
        r.verify(r.required("PROJECT") + "-" + service + "-1", "baseline-sources.json", assets_match="baseline-assets.json")
    r.prepare_metadata_permissions()
    r.save("all-source-evidence.json", {"base_id": base_id, "sources": records, "baseline_build_outputs": allowed_by_app,
        "inputs_sha256": r.sha(inputs_path), "scripts": r.scripts(), "clean_tools": {name: r.sha(ROOT / name) for name in CLEAN_TOOLS},
        "prepared_files": {name: r.sha(ROOT / name) for name in PREPARED_FILES}})
    print("CLEAN_GIT_SOURCES_READY", flush=True)


def checked_inputs(r, inputs_path):
    evidence = r.load("all-source-evidence.json")
    assert evidence["inputs_sha256"] == r.sha(inputs_path), "release_inputs_changed"
    assert evidence["scripts"] == r.scripts(), "runner_changed"
    assert evidence["clean_tools"] == {name: r.sha(ROOT / name) for name in CLEAN_TOOLS}, "clean_tools_changed"
    assert evidence["prepared_files"] == {name: r.sha(ROOT / name) for name in PREPARED_FILES}, "prepared_source_manifest_changed"
    assert r.image_id(r.required("BASE_IMAGE")) == evidence["base_id"], "baseline_image_changed"
    for app, source in evidence["sources"].items():
        repo = Path("source-repos") / app
        assert r.run(["git", "-C", repo, "rev-parse", "HEAD"], capture=True) == source["revision"]
        assert not r.run(["git", "-C", repo, "status", "--porcelain", "--untracked-files=all"], capture=True), "dirty_source:" + app
    return evidence


def finalize(r, inputs_path):
    evidence = checked_inputs(r, inputs_path)
    for app, source in evidence["sources"].items():
        head = r.run(["git", "ls-remote", source["remote"], "refs/heads/" + source["branch"]], capture=True).split()
        assert head and head[0] == source["revision"], "remote_head_changed:" + app
    base, candidate = r.required("BASE_IMAGE"), r.required("NEW_IMAGE")
    r.running(base, evidence["base_id"])
    r.verify(base, "baseline-sources.json", image=True, assets_match="baseline-assets.json")
    for service in ("backend", "frontend"):
        r.verify(r.required("PROJECT") + "-" + service + "-1", "baseline-sources.json", assets_match="baseline-assets.json")
    r.verify(candidate, "candidate-sources.json", image=True, assets_out="candidate-assets.json")
    labels = r.inspection(candidate)["Config"].get("Labels") or {}
    for app, source in evidence["sources"].items():
        assert labels.get(source["label"]) == source["revision"], "candidate_revision_label_mismatch:" + app
    assert labels.get("com.leya.release") == r.required("RELEASE_NAME"), "candidate_release_label_mismatch"
    evidence["candidate_git"] = json.loads(r.run(
        ["docker", "run", "--rm", "-i", "--network", "none", "--entrypoint", r.PYTHON, candidate, "-c", GIT_CHECK],
        capture=True, input=json.dumps(evidence["sources"]).encode()))
    code = "import frappe; frappe.init('', sites_path='/home/frappe/frappe-bench/sites'); from frappe.gettext.translate import get_translations_from_mo; t=get_translations_from_mo('zh','erpnext'); assert t.get('Pickup and Delivery Details'); print('ERPNEXT_GETTEXT_OK')"
    r.run(["docker", "run", "--rm", "--network", "none", "--workdir", r.SITES, "--entrypoint", r.PYTHON, candidate, "-c", code])
    evidence["candidate_id"] = r.image_id(candidate)
    r.save("all-source-evidence.json", evidence)
    build_base = r.required("BUILD_IMAGE")
    r.save("release-state.json", {"scripts": r.scripts(), "base_id": evidence["base_id"],
        "build_base_image": build_base, "build_base_id": r.image_id(build_base), "candidate_id": evidence["candidate_id"],
        "revisions": {app: source["revision"] for app, source in evidence["sources"].items() if app in {"erpnext", "sf_international", "erpnext_shipping", "flow"}}})
    print("CANDIDATE_IMAGE_READY; run the pinned runner rehearsal before deployment", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("sources", "build", "finalize"))
    parser.add_argument("--inputs", type=Path, default=ROOT / "release-inputs.json")
    args = parser.parse_args()
    inputs_path = args.inputs.resolve()
    inputs = json.loads(inputs_path.read_text())
    os.environ.update({key: value for key, value in inputs.items() if isinstance(value, str)})
    r, verifier = module("release"), module("verify_code")
    if args.stage == "sources":
        sources(r, verifier, inputs, inputs_path)
    elif args.stage == "build":
        evidence = checked_inputs(r, inputs_path)
        assert r.run(["docker", "image", "inspect", r.required("NEW_IMAGE")], capture=True, check=False).returncode != 0, "candidate_tag_already_exists"
        command = ["docker", "build", "--file", "Containerfile", "--tag", r.required("NEW_IMAGE")]
        for key in ("BUILD_IMAGE", "RUNTIME_IMAGE"):
            command += ["--build-arg", key + "=" + r.required(key)]
        for source in evidence["sources"].values():
            command += ["--label", source["label"] + "=" + source["revision"]]
        command += ["--label", "com.leya.release=" + r.required("RELEASE_NAME"), "."]
        r.run(command)
        finalize(r, inputs_path)
    else:
        finalize(r, inputs_path)


if __name__ == "__main__":
    main()
