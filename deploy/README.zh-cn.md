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

The image build fails unless every path in `assets.json` exists and the required
Simplified Chinese translations are present in the compiled MO files. Verify the
finished image before deployment:

```bash
docker run --rm \
  --entrypoint /home/frappe/frappe-bench/env/bin/python \
  "$ZH_IMAGE" \
  apps/erpnext/deploy/verify_zh_cn_image.py
```

Application assets are immutable image contents. Never run `bench build` in a
production application container because each service has its own image layer.
Rebuild and verify a new image, then recreate all application services together.
