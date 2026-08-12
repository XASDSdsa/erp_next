# Simplified Chinese production image

This image must be built from the exact commits in the two application repositories. It replaces application source, templates, translations, and compiled assets together; copying only `zh.po` files is not sufficient for strings that require source-level translation calls.

Expected build context:

```text
build/zh-cn/
  frappe-source/   # git@github.com:XASDSdsa/frappe.git
  erpnext-source/  # git@github.com:XASDSdsa/erp_next.git
```

Build from `/opt/leya-erpnext-v16`:

```bash
docker build \
  --build-arg BASE_IMAGE="$BASE_IMAGE" \
  --file build/zh-cn/erpnext-source/deploy/Containerfile.zh-cn \
  --tag "$ZH_IMAGE" \
  build/zh-cn
```

Always pin and verify both Git commits before building. Deploy the resulting image to backend, frontend, websocket, queue, and scheduler services together.
