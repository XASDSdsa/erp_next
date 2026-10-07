#!/usr/bin/env python3
"""Source and asset verification for the clean Git image."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile

BENCH_PYTHON = "/home/frappe/frappe-bench/env/bin/python"
REMOTE_CHECK = r'''
import hashlib,json,pathlib,sys
request=json.load(sys.stdin)
root=pathlib.Path('/home/frappe/frappe-bench')
checked={}
expected_apps=set(request['sources'])
actual_apps={path.name for path in (root/'apps').iterdir() if path.is_dir() and not path.name.startswith('.')}
assert actual_apps == expected_apps, ('unexpected_application_set', sorted(actual_apps), sorted(expected_apps))
apps_file=root/'sites/apps.txt'
assert apps_file.is_file(), 'missing_apps_txt'
installed_apps={line.strip() for line in apps_file.read_text().splitlines() if line.strip()}
assert installed_apps == expected_apps, ('unexpected_installed_application_set', sorted(installed_apps), sorted(expected_apps))
for app,data in request['sources'].items():
    app_root=root/'apps'/app
    for name,expected in data['files'].items():
        path=app_root/name
        assert path.is_file() or path.is_symlink(), ('missing',app,name)
        value=str(path.readlink()).encode() if path.is_symlink() else path.read_bytes()
        assert hashlib.sha256(value).hexdigest()==expected, ('source_drift',app,name)
    checked[app]=len(data['files'])
assets={}
for filename in ('assets.json','assets-rtl.json'):
    manifest_path=root/'assets'/filename
    assert manifest_path.is_file(), ('missing_manifest',filename)
    for name,url in json.loads(manifest_path.read_text()).items():
        assert url.startswith('/assets/'), ('unexpected_asset_url',name,url)
        path=root/'assets'/url.removeprefix('/assets/')
        assert path.is_file(), ('missing_asset',name,url)
        assets[filename+':'+name]={'url':url,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
print(json.dumps({'checked_sources':checked,'assets':assets},sort_keys=True))
'''


def run(args, **kwargs):
    return subprocess.check_output(args, **kwargs)


def archive_manifest(repo, sha):
    data = run(["git", "-C", str(repo), "archive", sha])
    files = {}
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        for member in archive.getmembers():
            assert not member.name.startswith("/") and ".." not in Path(member.name).parts
            if member.isfile():
                value = archive.extractfile(member).read()
            elif member.issym():
                value = member.linkname.encode()
            elif member.isdir():
                continue
            else:
                raise AssertionError(("unsupported_git_entry", member.name))
            files[member.name] = hashlib.sha256(value).hexdigest()
    assert files, ("empty_source", str(repo), sha)
    return {"sha": sha, "files": files}


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="mode", required=True)
    for mode in ("image", "container"):
        check = sub.add_parser(mode)
        check.add_argument("target")
        check.add_argument("manifest", type=Path)
        check.add_argument("--assets-out", type=Path)
        check.add_argument("--assets-match", type=Path)
    args = parser.parse_args()
    sources = json.loads(args.manifest.read_text())
    inspection = json.loads(run(["docker", "inspect", args.target]))[0]
    labels = inspection["Config"].get("Labels") or {}
    for app, data in sources.items():
        alias = {"erpnext_shipping": "shipping"}.get(app, app)
        label = "org.erpnext." + alias + "-revision"
        assert labels.get(label) == data["sha"], ("revision_label_mismatch", app)
    command = (["docker", "run", "--rm", "-i", "--network", "none", "--entrypoint", BENCH_PYTHON, args.target]
               if args.mode == "image" else ["docker", "exec", "-i", args.target, BENCH_PYTHON])
    result = json.loads(run(command + ["-c", REMOTE_CHECK], input=json.dumps({"sources": sources}).encode()))
    assets = result["assets"]
    if args.assets_match:
        assert assets == json.loads(args.assets_match.read_text()), "asset_manifest_or_hash_changed"
    if args.assets_out:
        args.assets_out.write_text(json.dumps(assets, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"result": "SOURCE_ASSETS_OK", "target": args.target,
                      "checked_sources": result["checked_sources"], "asset_count": len(assets)}))


if __name__ == "__main__":
    main()
