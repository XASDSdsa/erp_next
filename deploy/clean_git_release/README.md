# Clean Git release image

This Containerfile follows the official [Frappe layered image](https://github.com/frappe/frappe_docker/blob/main/images/layered/Containerfile): initialize a new bench in the build image, then copy that complete bench into the runtime image. It installs source and dependencies and builds assets through native `bench init`.

The caller must supply `BUILD_IMAGE=frappe/build@sha256:<64 lowercase hex digits>` and `RUNTIME_IMAGE=frappe/base@sha256:<64 lowercase hex digits>`. Both must be compatible official images, fixed by digest. No previous application image, exported container, imported filesystem, or production volume is a build input.

Prepare this build context on the host:

```text
Containerfile
apps.json
source-remotes.json
source-repos/
  frappe/        # Git repository at the selected Frappe commit
  erpnext/       # Includes deploy/shipment_carrier_isolation/assets-entrypoint.sh
  flow/
  ...            # Every other application selected for this release
```

Fetch each owning repository through its configured SSH remote. Resolve the requested latest branch to an exact commit, check out that commit, and create the local `release-source` branch at that same commit. Record every repository URL, source branch, commit SHA, and both image digests in the release manifest before building. Include each checkout's `.git` in the context; do not exclude it with `.dockerignore` or include local credentials, SSH keys, business data, or production site configuration.

Generate `apps.json` from that manifest for every app except Frappe, which has its own `bench init` arguments:

```json
[
  {"url": "file:///opt/git/erpnext", "branch": "release-source"},
  {"url": "file:///opt/git/flow", "branch": "release-source"}
]
```

The example list is not the complete production app list. The release caller must include all required runtime apps and respect their dependencies. The retired standalone SF app is fetched only as a migration source and is deliberately excluded from generated `apps.json`; it must never be installed in the candidate bench. SF carrier behavior belongs to Shipping's `erpnext_shipping.sf_international` module. Preserve historic documents, credentials, waybills, and accounting records.

Both stages install `libgl1` and `libglib2.0-0` for Flow's native dependencies. The final image contains only the newly built bench plus the official runtime base and these system packages. All application `.git` directories and app `public/dist` outputs remain available for verification. The generated `sites/assets` directory moves intact to `bench/assets`; the existing ERPNext entrypoint atomically links it into the mounted sites volume and executes the configured service.

Build with the manifest's digest values, for example:

```sh
docker build --file Containerfile \
  --build-arg BUILD_IMAGE="$BUILD_IMAGE" \
  --build-arg RUNTIME_IMAGE="$RUNTIME_IMAGE" \
  --tag "$CANDIDATE_IMAGE" .
```

Before deployment, verify every application's runtime Git HEAD against the release manifest, all asset-manifest files against actual image files, and the candidate's dependencies and service startup in isolation. This file builds a candidate; it does not authorize or perform a production switch.

## Pinned release preparation

Archive Shipping's `deploy/sf_provider_migration` runner from the selected Shipping Git commit into a fresh release directory. Place this directory's `prepare.py`, `Containerfile`, and `README.md` beside that runner, byte for byte from the selected ERPNext commit. Keep the runner's original `Dockerfile`, `.dockerignore`, and all other tool files unchanged. The preparer checks both sets against their owning Git commits.

Write `release-inputs.json` with the existing runner environment values plus `BASE_IMAGE_ID`, `BUILD_IMAGE`, `RUNTIME_IMAGE`, and each app's SSH remote, branch, target SHA, and baseline SHA. Additional prefixes are `FRAPPE`, `PAYMENTS`, `CRM`, and `INSIGHTS`, each using `<PREFIX>_BASE_REV`; the original four apps retain the runner's `BASE_ERPNEXT_REV`, `BASE_SF_REV`, `BASE_SHIPPING_REV`, and `BASE_FLOW_REV` names. Payments requires a separately confirmed baseline commit.

The confirmed historic CRM build outputs may be supplied as `CRM_BASE_BUILD_OUTPUTS`, mapping each exact tracked path to `git_sha256` and `built_sha256`, together with `CRM_BASE_BUILD_REV`. The preparer checks the original Git file, revision, and immutable baseline image before using those hashes for baseline verification only. It saves the untouched Git manifest separately. Candidate files always use the target Git hashes; any new generated-source drift fails verification for diagnosis.

Run these explicit stages from the release directory:

```sh
python3 prepare.py sources
python3 prepare.py build
```

`sources` fetches and pins all eight repositories, checks the unchanged running baseline, generates both source manifests and app/remotes inputs, validates the Compose override, and prepares Flow's Git-owned metadata script permissions. `build` performs the clean build and then runs `finalize` automatically. If a finished build needs inspection before finalization, `python3 prepare.py finalize` repeats the verification without rebuilding, including checking that all eight remote branch heads still match the selected commits.

The runner-compatible `release-state.json` retains its four revision keys. `all-source-evidence.json` additionally records all eight source repositories, input/tool hashes, candidate Git HEAD/remotes, and immutable image IDs. New assets are checked against their own complete manifests; full rebuilds are not constrained to the old baseline asset hashes. The existing runner still performs isolated rehearsal and controlled deployment.
