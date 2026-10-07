# Clean Git release image

This is the only supported build path for a fresh application image. It starts
from official Frappe build and runtime images pinned by immutable digests,
checks out the exact Git commits requested for every runtime app, runs a new
bench build, and copies that bench into the runtime image.

The build accepts only the pinned Git source manifest and official immutable
build/runtime images. It does not read application images, exported filesystems,
production volumes, database state, or production configuration. Existing
business data is untouched because this procedure has no database stage.

Required sources are Frappe, Payments, ERPNext, ERPNext Shipping, CRM, Insights,
and Flow. Payments uses the official `frappe/payments` repository; all other
sources use the configured user-owned SSH repositories. Every source is pinned
to a branch and exact commit in `release-inputs.json`.

The selected Shipping repository is installed as the `erpnext_shipping` runtime
package. The build helper files are owned by ERPNext and are independent of any
retired application or migration directory.

Run the build-only stages from a fresh release directory:

```sh
python3 prepare.py sources
python3 prepare.py build
```

`build` finalizes the same candidate automatically. `python3 prepare.py
finalize` only rechecks an already-built candidate; it does not rebuild,
migrate, or deploy. The generated image receives neutral revision labels and a
release label. The command stops before any server or business-data mutation.
