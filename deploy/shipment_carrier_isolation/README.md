# Shipment carrier isolation release

This ERPNext-owned procedure handles exactly ERPNext, SF and ERPNext Shipping.
It is based on the complete, verified r5 image. It does not install dependencies,
run a full migration, edit live application source, or restart MariaDB/Redis.

Commit and push all three repositories first. Fetch this directory from the
**same ERP_REV** used to build the application into a new server release directory.
The tool compares its bytes to that Git commit and refuses reused release state
or image tags. Run from that directory with GitHub SSH access:

```sh
export ERP_REV=<40-character-pushed-ERPNext-commit>
export SF_REV=<40-character-pushed-SF-commit>
export SHIPPING_REV=<40-character-pushed-Shipping-commit>
export RELEASE_NAME=shipment-carrier-isolation-20260929-r1
export NEW_IMAGE=leya/erpnext:v16.36.0-shipment-carrier-isolation-20260929-r1
bash release.sh prepare
export BACKUP_DIR=/opt/leya-erpnext-v16/build/production-backups/sf-region-lifecycle-20260929-r5
bash release.sh rehearse
export PRODUCTION_NETWORK=<network-confirmed-on-current-backend-and-database>
bash release.sh deploy
```

`release.env` fixes the verified baseline commits and DB/Redis image IDs. Target
branch tips must equal the three target commits at preparation and deployment.
Shipping baseline is **3a888e6**, its recorded production source; 5f852ca only
removed a CI workflow. Baseline checks compare every tracked file, without
skipping that workflow or substituting a newer SHA.

Preparation inherits r5 dependencies, overlays exact Git archives, removes all
Git-deleted files, and runs `bench build --app erpnext --production --force`.
Current Frappe forwards `force=True` to gettext compilation, so archived PO
timestamps cannot leave old MO files. Shipping's changed form script is loaded
directly, so no Shipping bundle build is needed; a changed Shipping public file
other than that script deliberately rejects this build plan. SF assets and all
other app assets must remain byte-identical to r5. All three source manifests,
deleted files, revision labels and every referenced asset are checked in an
immutable candidate. The historical baseline banking lockfile exception is
pinned by both original Git and observed build-output hashes; the new candidate
uses its exact Git lockfile and performs no install.

Rehearsal creates one **internal** Docker network with fresh MariaDB, Redis and
sites/logs/DB volumes. It imports only the backup copy; no production mount,
network, worker, scheduler or published port is used. All DB/Redis settings in
the copied site configuration are replaced by isolated endpoints. The backup
encryption key stays in protected files and is never printed. Isolation assets
and stopped containers are retained for inspection after success or failure.

The real restored database must pass: r5 metadata snapshot; candidate upgrade;
native fields, effective Sales User permissions, provider choices, unique index,
native document onload and unchanged business rows/schema; identical second
migration; exact metadata restoration on r5; injected failure immediately after
the first DocType reload; another exact restoration; successful upgrade again.
Only then is `rehearsal.ok.json` written. This is a metadata rehearsal, not proof
of external carrier success. Complete the requested UI and isolated business
regressions before invoking deployment.

Failure injection rejects a DB host without the `shipment-check-` prefix before
Frappe initializes or connects. Its Docker wrapper additionally requires an
internal network and sites/logs volume names with that prefix. It cannot be used
against the production `db` alias. Production's verified network is
`frappe_docker_default`; supply that exact network after checking the live mounts.

Production sets maintenance, stops frontend/WebSocket ingress and scheduling,
and suspends dequeue using the verified RQ 2.6.1 API without an expiring TTL.
It then gracefully stops the backend and invokes
`docker compose stop --timeout -1 queue-long queue-short`. RQ handles the first
SIGTERM as warm shutdown, finishing each current job; Docker waits indefinitely
without SIGKILL and suppresses automatic container restart. Do not send a second
signal or repeat the command while a long job is finishing. Waiting only for an
idle/suspended state is insufficient because RQ can remain in blocking dequeue.
The release refuses a pre-existing queue suspension and records its own Redis
owner marker so recovery cannot resume another operator's pause.

All six app containers must be stopped without forced termination before a
fresh full backup or metadata snapshot. Disposable r5 containers use the
verified production network and the backend's exact sites/logs volumes to run
Bench backup and metadata operations. Backup copies are read directly from the
sites volume with read-only mounts in a networkless container; they do not
depend on a stopped backend process or its writable layer. The approved backup
location is `private/deployment-backups/<release>`. The only migration entry
points are `erpnext.patches.v16_0.move_shipment_carrier_metadata.execute` and
`sf_international.install.ensure_sf_shipment_metadata`. Source, asset, metadata,
business, active service version and startup log checks run while maintenance
is on and all background containers remain stopped. Queues restart after the
full internal/public health window and resume explicitly after startup checks.
No full `migrate`, SF installer, business-data backfill or DB restart is
part of this procedure.

The snapshot contains the three DocType rows and all native child metadata,
Custom Fields, Property Setters, Custom DocPerms, and associated DocType Layouts.
It includes the Sales Order List View Settings row and only the two known Sales
Order list Client Scripts, preserving saved column widths/order and old script
state when rolling back the corresponding migration.
It also records business-row and schema/index hashes. Rollback first stops app
processes, then uses the r5 Frappe runtime and the saved operational helper to
restore **exact metadata rows without hooks or business-data restoration**.
It verifies the restored snapshot before restarting r5. Because schema sync
commits partway through, a database transaction alone is not a rollback.
Changed business rows/schema or any failed restore check blocks automatic
recovery; keep maintenance and diagnose rather than restore the entire DB.

Automatic metadata rollback applies only before maintenance-off is attempted.
Once traffic can write business records, any health or background startup/log
failure retains the verified candidate and running containers, requests
maintenance again, and writes `deployment.incomplete.json`. It never restores
the pre-release metadata snapshot or blindly stops live workers at that point.
The file records the phase and whether dequeue remains paused. Diagnose the
preserved health/log evidence before reopening traffic or explicitly resuming
that release's queue pause; a maintenance flag can take up to 60 seconds to reach
existing HTTP workers. Such an attempt is incomplete, never a successful release.

The script preserves the previous Compose override, baseline image and all
evidence. It uses the verified internal Host header, public Mozilla/5.0 header,
exact pong and 61-second maintenance-cache window. Review each failure log and
correct its demonstrated cause before making a new release/tag. Do not rerun a
failed release unchanged. Record actual commands, commits, image IDs, backups,
snapshots, migrations, health and rollback results in DEPLOYMENT_NOTES.md.

Local delivery verification is limited to Python/Bash syntax and static review.
No candidate build, restored database rehearsal or production execution is
claimed until those commands have completed with their explicit result files.
