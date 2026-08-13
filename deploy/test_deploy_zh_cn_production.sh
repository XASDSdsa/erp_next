#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEST_ROOT="$(mktemp -d)"
MOCK_BIN="$TEST_ROOT/bin"
STATE_DIR="$TEST_ROOT/state"
LOG_FILE="$TEST_ROOT/calls.log"
KEEP_TEST_ROOT="${KEEP_TEST_ROOT:-0}"

cleanup() {
	if [[ "$KEEP_TEST_ROOT" -eq 1 ]]; then
		echo "test_root=$TEST_ROOT"
	else
		rm -rf "$TEST_ROOT"
	fi
}
trap cleanup EXIT

mkdir -p "$MOCK_BIN" "$STATE_DIR"

cat >"$MOCK_BIN/docker" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

echo "docker $*" >>"$MOCK_LOG_FILE"

if [[ "$1 $2" == "image inspect" ]]; then
	exit 0
fi

if [[ "$1" == "run" ]]; then
	echo "ZH_CN_IMAGE_VERIFICATION_OK assets=1 translations=10"
	exit 0
fi

if [[ "$1" == "compose" && " $* " == *" config --format json "* ]]; then
	python3 - <<PY
import json
import os

image = os.environ["ZH_IMAGE"]
services = {
    name: {"image": image}
    for name in ("backend", "websocket", "frontend", "queue-long", "queue-short", "scheduler")
}
print(json.dumps({"services": services}))
PY
	exit 0
fi

if [[ "$1" == "compose" && " $* " == *" up -d "* ]]; then
	printf '%s' "$ZH_IMAGE" >"$MOCK_STATE_DIR/current_image"
	exit 0
fi

if [[ "$1" == "inspect" ]]; then
	image="$(cat "$MOCK_STATE_DIR/current_image")"
	format="${*: -1}"
	if [[ "$format" == *".State.Running"* && "$format" == *".Config.Image"* ]]; then
		if [[ "$format" == container=* ]]; then
			echo "container=/$2 image=$image running=true restart_count=0"
		else
			echo "$image true"
		fi
	else
		echo "$image"
	fi
	exit 0
fi

if [[ "$1" == "exec" ]]; then
	if [[ " $* " == *" frappe.translate.clear_cache "* ]]; then
		touch "$MOCK_STATE_DIR/cache_cleared"
		echo "cache_cleared" >>"$MOCK_LOG_FILE"
		exit 0
	fi

	if [[ " $* " == *" frappe.translate.get_all_translations "* ]]; then
		echo "runtime_translation_read" >>"$MOCK_LOG_FILE"
		if [[ "${MOCK_FAIL_RUNTIME:-0}" -eq 1 || ! -f "$MOCK_STATE_DIR/cache_cleared" ]]; then
			echo '{}'
		else
			cat "$MOCK_STATE_DIR/translations.json"
		fi
		exit 0
	fi

	if [[ "$*" == *" cat /home/frappe/frappe-bench/assets/assets.json"* ]]; then
		cat "$MOCK_STATE_DIR/assets.json"
		exit 0
	fi

	if [[ " $* " == *" verify_zh_cn_image.py "* ]]; then
		echo "ZH_CN_IMAGE_VERIFICATION_OK assets=1 translations=10"
		exit 0
	fi

	exit 0
fi

echo "Unexpected docker call: $*" >&2
exit 1
EOF

cat >"$MOCK_BIN/curl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

echo "curl $*" >>"$MOCK_LOG_FILE"

if [[ "$*" == *"/assets/assets.json"* ]]; then
	echo "curl_assets_match" >>"$MOCK_LOG_FILE"
	cat "$MOCK_STATE_DIR/assets.json"
elif [[ " $* " == *" -w "* ]]; then
	echo "curl_content_type_match" >>"$MOCK_LOG_FILE"
	printf 'application/javascript'
else
	printf '{"message":"pong"}'
fi
EOF

chmod +x "$MOCK_BIN/docker" "$MOCK_BIN/curl"

cat >"$STATE_DIR/translations.json" <<'EOF'
{
  "Brand": "品牌",
  "List View": "列表视图",
  "Saved Filters": "已保存的筛选器",
  "Getting Started": "入门指南",
  "Create Warehouses": "创建仓库",
  "Create Item": "创建物料",
  "Create Purchase Receipt": "创建采购入库单",
  "Create Transfer Entry": "创建库存调拨单",
  "View Stock Balance": "查看库存余额",
  "Review Stock Settings": "查看库存设置"
}
EOF

cat >"$STATE_DIR/assets.json" <<'EOF'
{"desk.bundle.js":"/assets/frappe/dist/js/desk.bundle.TEST.js"}
EOF

run_deploy() {
	local fail_runtime="$1"
	local output_file="$2"
	local path_value="$MOCK_BIN:$PATH"

	printf '%s' 'leya/erpnext:stable' >"$STATE_DIR/current_image"
	rm -f "$STATE_DIR/cache_cleared"
	: >"$LOG_FILE"

	export PATH="$path_value"
	export MOCK_LOG_FILE="$LOG_FILE"
	export MOCK_STATE_DIR="$STATE_DIR"
	export MOCK_FAIL_RUNTIME="$fail_runtime"
	export ZH_IMAGE='leya/erpnext:zh-verified'
	export SITE='erp.example.test'
	export PROJECT='leya-erpnext-v16'
	export PUBLIC_BASE_URL='https://erp.example.test'

	bash "$SCRIPT_DIR/deploy_zh_cn_production.sh" >"$output_file" 2>&1
}

SUCCESS_OUTPUT="$TEST_ROOT/success.log"
run_deploy 0 "$SUCCESS_OUTPUT"

grep -F 'ZH_CN_RUNTIME_TRANSLATIONS_OK' "$SUCCESS_OUTPUT" >/dev/null
grep -F 'ZH_CN_PRODUCTION_DEPLOYED_AND_VERIFIED image=leya/erpnext:zh-verified' "$SUCCESS_OUTPUT" >/dev/null
test "$(cat "$STATE_DIR/current_image")" = 'leya/erpnext:zh-verified'

CLEAR_LINE="$(grep -n '^cache_cleared$' "$LOG_FILE" | head -1 | cut -d: -f1)"
READ_LINE="$(grep -n '^runtime_translation_read$' "$LOG_FILE" | head -1 | cut -d: -f1)"
test "$CLEAR_LINE" -lt "$READ_LINE"

FAILURE_OUTPUT="$TEST_ROOT/failure.log"
if run_deploy 1 "$FAILURE_OUTPUT"; then
	echo "Expected the simulated runtime translation failure to fail deployment" >&2
	exit 1
fi

grep -F 'ZH_CN_DEPLOY_FAILED_ROLLING_BACK' "$FAILURE_OUTPUT" >/dev/null
grep -F 'ROLLBACK_FINISHED image=leya/erpnext:stable' "$FAILURE_OUTPUT" >/dev/null
test "$(cat "$STATE_DIR/current_image")" = 'leya/erpnext:stable'

echo DEPLOY_ZH_CN_PRODUCTION_TEST_OK
