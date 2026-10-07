#!/usr/bin/env python3
"""Prepare one clean Git release from exact Git repositories."""
import argparse
import importlib.util
import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APPS = (
    ("FRAPPE", "frappe", "frappe"),
    ("PAYMENTS", "payments", "payments"),
    ("ERP", "erpnext", "erpnext"),
    ("SHIPPING", "erpnext_shipping", "shipping"),
    ("CRM", "crm", "crm"),
    ("INSIGHTS", "insights", "insights"),
    ("FLOW", "flow", "flow"),
)
CLEAN_TOOLS = ("prepare.py", "release.py", "verify_code.py", "Containerfile", "README.md", "assets-entrypoint.sh")
PREPARED_FILES = ("candidate-sources.json", "apps.json", "source-remotes.json", "Containerfile.dockerignore")
REMOTES = {
    "frappe": "git@github.com:XASDSdsa/frappe.git",
    "payments": "https://github.com/frappe/payments.git",
    "erpnext": "git@github.com:XASDSdsa/erp_next.git",
    "erpnext_shipping": "git@github.com:XASDSdsa/erpnext-shipping.git",
    "crm": "git@github.com:XASDSdsa/frappe-crm.git",
    "insights": "git@github.com:XASDSdsa/frappe-insights.git",
    "flow": "git@github.com:XASDSdsa/frappe-flow.git",
}


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


def validate_inputs(inputs):
    keys = {"NEW_IMAGE", "RELEASE_NAME", "BUILD_IMAGE", "RUNTIME_IMAGE"}
    keys.update(prefix + suffix for prefix, _, _ in APPS for suffix in ("_REMOTE", "_BRANCH", "_REV"))
    assert set(inputs) == keys, "unexpected_or_missing_release_inputs:" + str(sorted(set(inputs) ^ keys))
    assert all(isinstance(value, str) and value.strip() == value and value for value in inputs.values()), "invalid_release_input"
    for key, image in (("BUILD_IMAGE", "build"), ("RUNTIME_IMAGE", "base")):
        assert re.fullmatch(r"frappe/" + image + r"@sha256:[0-9a-f]{64}", inputs[key]), "official_digest_required:" + key
    assert re.fullmatch(r"[a-z0-9][a-z0-9_.-]*:[A-Za-z0-9_][A-Za-z0-9_.-]*", inputs["NEW_IMAGE"]), "local_candidate_image_tag_required"
    for prefix, app, _ in APPS:
        assert inputs[prefix + "_REMOTE"] == REMOTES[app], "wrong_source_repository:" + app
        assert re.fullmatch(r"[0-9a-f]{40}", inputs[prefix + "_REV"]), "invalid_revision:" + app


def sources(r, verifier, inputs, inputs_path):
    assert not Path("release-state.json").exists() and not Path("all-source-evidence.json").exists(), "use_new_release_directory"
    repo_root = Path("source-repos")
    repo_root.mkdir(exist_ok=True)
    assert {p.name for p in repo_root.iterdir()} <= {app for _, app, _ in APPS}, "unexpected_source_directory"
    target, records = {}, {}
    for prefix, app, alias in APPS:
        remote, branch, revision = (inputs[prefix + suffix] for suffix in ("_REMOTE", "_BRANCH", "_REV"))
        r.run(["git", "check-ref-format", "refs/heads/" + branch], capture=True)
        head = r.run(["git", "ls-remote", remote, "refs/heads/" + branch], capture=True).split()
        assert head and head[0] == revision, "remote_head_changed:" + app
        repo = repo_root / app
        if not repo.exists():
            r.run(["git", "init", repo])
            r.run(["git", "-C", repo, "remote", "add", "origin", remote])
            r.run(["git", "-C", repo, "fetch", "--depth=1", "origin", revision])
            r.run(["git", "-C", repo, "checkout", "-b", "release-source", revision])
        assert not repo.is_symlink(), "source_must_be_checkout:" + app
        assert r.run(["git", "-C", repo, "remote", "get-url", "origin"], capture=True) == remote
        assert r.run(["git", "-C", repo, "rev-parse", "HEAD"], capture=True) == revision
        assert r.run(["git", "-C", repo, "branch", "--show-current"], capture=True) == "release-source"
        assert not r.run(["git", "-C", repo, "status", "--porcelain", "--untracked-files=all", "--ignored"], capture=True), "dirty_source:" + app
        target[app] = verifier.archive_manifest(repo, revision)
        records[app] = {"remote": remote, "branch": branch, "revision": revision, "label": "org.erpnext." + alias + "-revision"}
    for name in CLEAN_TOOLS:
        assert (repo_root / "erpnext/deploy/clean_git_release" / name).read_bytes() == (ROOT / name).read_bytes(), "clean_tool_not_from_target_git:" + name
    r.save("candidate-sources.json", target)
    r.save("apps.json", [{"url": "file:///opt/git/" + app, "branch": "release-source"} for _, app, _ in APPS if app != "frappe"])
    r.save("source-remotes.json", {app: source["remote"] for app, source in records.items()})
    includes = [pattern for _, app, _ in APPS for pattern in ("!source-repos/" + app + "/", "!source-repos/" + app + "/**")]
    Path("Containerfile.dockerignore").write_text("\n".join(["**", "!source-repos/", *includes, "!apps.json", "!source-remotes.json", "!assets-entrypoint.sh", ""]))
    r.save("all-source-evidence.json", {"sources": records, "inputs_sha256": r.sha(inputs_path), "scripts": r.scripts(),
        "prepared_files": {name: r.sha(ROOT / name) for name in PREPARED_FILES}})
    print("CLEAN_GIT_SOURCES_READY", flush=True)


def checked_inputs(r, inputs_path):
    evidence = r.load("all-source-evidence.json")
    assert evidence["inputs_sha256"] == r.sha(inputs_path), "release_inputs_changed"
    assert evidence["scripts"] == r.scripts(), "runner_changed"
    assert evidence["prepared_files"] == {name: r.sha(ROOT / name) for name in PREPARED_FILES}, "prepared_source_manifest_changed"
    assert {p.name for p in Path("source-repos").iterdir()} == set(evidence["sources"]), "unexpected_source_directory"
    for app, source in evidence["sources"].items():
        repo = Path("source-repos") / app
        assert not repo.is_symlink(), "source_must_be_checkout:" + app
        assert r.run(["git", "-C", repo, "remote", "get-url", "origin"], capture=True) == source["remote"]
        assert r.run(["git", "-C", repo, "branch", "--show-current"], capture=True) == "release-source"
        assert r.run(["git", "-C", repo, "rev-parse", "HEAD"], capture=True) == source["revision"]
        assert not r.run(["git", "-C", repo, "status", "--porcelain", "--untracked-files=all", "--ignored"], capture=True), "dirty_source:" + app
    return evidence


def finalize(r, inputs_path):
    evidence = checked_inputs(r, inputs_path)
    candidate = r.required("NEW_IMAGE")
    r.verify(candidate, "candidate-sources.json", image=True, assets_out="candidate-assets.json")
    r.verify_startup(candidate, image=True)
    labels = r.inspection(candidate)["Config"].get("Labels") or {}
    for app, source in evidence["sources"].items():
        assert labels.get(source["label"]) == source["revision"], "candidate_revision_label_mismatch:" + app
    assert labels.get("com.erpnext.release") == r.required("RELEASE_NAME"), "candidate_release_label_mismatch"
    evidence["candidate_git"] = json.loads(r.run(
        ["docker", "run", "--rm", "-i", "--network", "none", "--entrypoint", r.PYTHON, candidate, "-c", GIT_CHECK],
        capture=True, input=json.dumps(evidence["sources"]).encode()))
    code = "import frappe; frappe.init('', sites_path='/home/frappe/frappe-bench/sites'); from frappe.gettext.translate import get_translations_from_mo; t=get_translations_from_mo('zh','erpnext'); assert t.get('Pickup and Delivery Details'); print('ERPNEXT_GETTEXT_OK')"
    r.run(["docker", "run", "--rm", "--network", "none", "--workdir", r.SITES, "--entrypoint", r.PYTHON, candidate, "-c", code])
    evidence["candidate_id"] = r.image_id(candidate)
    r.save("all-source-evidence.json", evidence)
    r.save("release-state.json", {"candidate_id": evidence["candidate_id"],
        "build_image": r.required("BUILD_IMAGE"), "runtime_image": r.required("RUNTIME_IMAGE"),
        "revisions": {app: source["revision"] for app, source in evidence["sources"].items()}})
    print("CANDIDATE_IMAGE_READY; build-only release; no migration or deployment was run", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("sources", "build", "finalize"))
    parser.add_argument("--inputs", type=Path, default=ROOT / "release-inputs.json")
    args = parser.parse_args()
    inputs_path = args.inputs.resolve()
    inputs = json.loads(inputs_path.read_text())
    validate_inputs(inputs)
    os.chdir(ROOT)
    os.environ.update({key: value for key, value in inputs.items() if isinstance(value, str)})
    r, verifier = module("release"), module("verify_code")
    if args.stage == "sources":
        sources(r, verifier, inputs, inputs_path)
    elif args.stage == "build":
        evidence = checked_inputs(r, inputs_path)
        assert r.run(["docker", "image", "inspect", r.required("NEW_IMAGE")], capture=True, check=False).returncode != 0, "candidate_tag_already_exists"
        command = ["docker", "build", "--pull", "--no-cache", "--file", "Containerfile", "--tag", r.required("NEW_IMAGE")]
        for key in ("BUILD_IMAGE", "RUNTIME_IMAGE"):
            command += ["--build-arg", key + "=" + r.required(key)]
        for source in evidence["sources"].values():
            command += ["--label", source["label"] + "=" + source["revision"]]
        command += ["--label", "com.erpnext.release=" + r.required("RELEASE_NAME"), "."]
        r.run(command)
        finalize(r, inputs_path)
    else:
        finalize(r, inputs_path)


if __name__ == "__main__":
    main()
