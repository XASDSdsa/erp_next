# Bounded source-only releases

`prepare_pinned_release.py RELEASE_DIRECTORY` prepares a candidate for the
existing Git-pinned release runner, including its isolated rehearsal, backup,
graceful switch and rollback gates. Run its `verify` mode immediately before
`release.sh deploy` to recheck all seven owning repository pins and Git identities.

This path is deliberately limited to dependency-compatible source updates with
Flow-only asset changes and no database migration. It requires the Flow-owned
`deploy/static_assets/metadata.py` read-only contract. Dependency changes are
rejected, and unrelated assets must retain their baseline hashes.

The build stage reuses the exact validated runtime dependencies. A legacy input
with more than four layers is flattened first using native Docker export/import
from a never-started disposable container, without production mounts. Flattening
only at the end is insufficient: the 421-layer input failed its first BuildKit
RUN with `mount options is too long`. The cached flattened input is tied to the
original immutable image ID; both stream exit statuses must succeed. The final
`FROM scratch` stage copies the completed filesystem, then restores and verifies
the original OCI runtime configuration. It does not inherit historical release
layers. A one-to-four-layer ceiling is enforced on every candidate. Source Git
archives, detached Git identities, image labels and runtime file hashes are
checked together; no live site volume is mounted for the build.

The legacy 2026-10-05 CRM image has stale Git metadata from an old two-file
source overlay. Its application files match a82db752; the two precisely pinned
baseline build outputs are auto-import declaration formatting and the nested
unfrozen postinstall's cropperjs lock resolution. Candidate archives and Git
identities replace these historical source differences. Existing CRM assets and
dependencies are preserved because this release does not change CRM runtime code.

Do not replace this with repeated single-stage `FROM <previous-production>`
overlays: that release pattern accumulated 421 filesystem layers by 2026-10-06.
Do not remove the previous production image until deployment verification passes.

Local check: `python3 -m unittest discover -s deploy -p test_build_bounded_image.py -v`.
