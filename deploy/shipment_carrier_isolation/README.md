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
Shipping baseline is **33ed4db56cd29b61a67e5b09df6cfb3783166709**, an archival
commit on `archive/r5-shipping-source-20260929` reproducing the immutable r5 source.
A read-only comparison of all 63 tracked files showed 62 match 5f852ca and
`utils.py` contains the historical unconditional delegation to SF's validator.
The latter SHA-256 is `184d51c898e0d0c5532981529900844cac49721221b7d0dc64f0e6b8818d97ef`
in both the immutable image and running backend. The earlier 3a888e6 source
record was inaccurate (it also included a now-absent CI workflow). Do not reuse
that record. Every source file is checked against the archive without exclusions.
The r5 Shipping revision label is absent; only its exact verified image ID may
use the archival source proof. The candidate must have all three revision labels.

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

The r5 startup wrapper removed and recreated the shared `sites/assets` symlink.
Concurrent service starts were observed to create `sites/assets/assets` and fail
with `File exists`, restarting the backend. The candidate's published
`assets-entrypoint.sh` creates a unique temporary symlink in the same directory
and atomically replaces the destination with `mv -Tf`; it refuses to erase a
real directory. The build context includes this script, and preparation plus
runtime backend/frontend checks verify its exact Git SHA-256 and executable mode.
All deployment, recovery and rollback starts run one service at a time, waiting
at most 15 seconds for its main process to finish the asset wrapper's `exec`.
With Docker init, the probe reads init's child; otherwise it reads PID 1. It
requires a nonempty command line without the wrapper's exact path. Existing
health checks then verify application behavior. This also prevents concurrent
asset initialization on the unchanged rollback image; recovery does not force
recreate already running workers.

Rehearsal creates one **internal** Docker network with fresh MariaDB, Redis and
sites/logs/DB volumes. It imports only the backup copy; no production mount,
network, worker, scheduler or published port is used. All DB/Redis settings in
the copied site configuration are replaced by isolated endpoints. The backup
encryption key stays in protected files and is never printed. Isolation assets
and stopped containers are retained for inspection after success or failure.
The isolated site tree includes `<site>/logs`; the volume initializer assigns
both sites and bench logs volumes to the image's `frappe` user. Direct Frappe
Python entry points run from `/home/frappe/frappe-bench/sites`, matching native
Bench behavior: Frappe logging resolves `../logs` and `<site>/logs` relative to
the process directory even when `frappe.init` has an explicit `sites_path`.
The metadata helper verifies both log directories before connecting to the DB;
it does not create runtime directories in production to repair an invalid setup.
Each metadata invocation clears the site's cached application hooks and current
process document-event map before invoking native global `frappe.clear_cache()`.
Baseline and candidate containers share that site's Redis, so changing images
alone cannot update `app_hooks`. The explicit hook eviction also ensures native
cache-clearing callbacks resolve from the running image. The same sequence runs
after migration commit before document onload validation and after exact metadata
restoration before its comparison. Standalone validate and baseline snapshot /
restore therefore cannot reuse the other image's document hooks. This performs
native cache invalidation; it does not run app installers or migration hooks.

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

Production sets maintenance and uses the same service shutdown procedure for
deployment and rollback. It marks frontend as manually stopped with Docker's
non-terminating `CONT` signal, then runs native `nginx -s quit` to close listeners.
It stops WebSocket next so existing upgraded connections can close, then waits
at most 120 seconds for frontend exit 0 with no restart. Scheduler receives
SIGINT, its verified Python/Click shutdown path (exit 1); backend receives TERM
with 120 seconds of grace. Each stage is checked before continuing.

The original r5 WebSocket runs Node as PID 1; its default signal handler resets
then re-raises TERM/INT, which Linux ignores for namespace init. Isolated tests
confirmed exit 137 after timeout, versus exit 143 with Docker init. One explicit
legacy retirement is allowed only after successful nginx QUIT, for the exact
immutable r5 image ID, original Node command and PID 1 argv, with Docker init
disabled. `docker stop --time 0` intentionally terminates that known legacy
WebSocket. Requested/completed evidence records its container ID and exit 137;
this exception cannot apply to other containers, OOM, backend, or workers.
The general exit-137 gate remains strict. No forced frontend termination occurs.

Before backend shutdown, dequeue is suspended using the verified RQ 2.6.1 API
without an expiring TTL. Workers receive TERM with `--time -1`. RQ handles the first
SIGTERM as warm shutdown, finishing each current job; Docker waits indefinitely
without SIGKILL and suppresses automatic container restart. Do not send a second
signal or repeat the command while a long job is finishing. Waiting only for an
idle/suspended state is insufficient because RQ can remain in blocking dequeue.
The release refuses a pre-existing queue suspension and records its own Redis
owner marker so recovery cannot resume another operator's pause.

All six app containers must be stopped and verified before a fresh full backup
or metadata snapshot, with only the exact legacy WebSocket exception above.
The candidate Compose override sets `init: true` for all six application services.
The pinned base image's declared PyYAML dependency parses the old override without
network or printing its contents; the published, hashed release script changes
only application image/init keys. A full resolved Compose comparison rejects any
other semantic change. Runtime checks require Docker `HostConfig.Init=true` for
every candidate application container. The exact original override bytes remain
available and are restored on rollback, including its original init settings.
Disposable r5 containers use the
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
