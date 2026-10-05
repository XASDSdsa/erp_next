# Bounded source-only releases

`prepare_pinned_release.py RELEASE_DIRECTORY` prepares a candidate for the
existing Git-pinned release runner, including its isolated rehearsal, backup,
graceful switch and rollback gates. Run its `verify` mode immediately before
`release.sh deploy` to recheck all seven owning repository pins and Git identities.

This path is deliberately limited to dependency-compatible source updates with
Flow-only asset changes and no database migration. It requires the Flow-owned
`deploy/static_assets/metadata.py` read-only contract. Dependency changes are
rejected, and unrelated assets must retain their baseline hashes.

The build stage reuses the exact validated runtime dependencies. The final
`FROM scratch` stage copies the completed filesystem, then restores and verifies
the original OCI runtime configuration. It does not inherit historical release
layers. A one-to-four-layer ceiling is enforced on every candidate. Source Git
archives, detached Git identities, image labels and runtime file hashes are
checked together; no live site volume is mounted for the build.

Do not replace this with repeated single-stage `FROM <previous-production>`
overlays: that release pattern accumulated 421 filesystem layers by 2026-10-06.
Do not remove the previous production image until deployment verification passes.

Local check: `python3 -m unittest discover -s deploy -p test_build_bounded_image.py -v`.
