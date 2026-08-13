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
	echo 'sha256:target-image-id'
	exit 0
fi

if [[ "$1" == "run" ]]; then
	echo "ZH_CN_IMAGE_VERIFICATION_OK assets=1 translations=10"
	exit 0
fi

if [[ "$1" == "cp" ]]; then
	target="${*: -1}"
	mkdir -p "$target"
	printf 'config' >"$target/20260813_site-site_config_backup.json"
	printf 'database' >"$target/20260813_site-database.sql.gz"
	if [[ "${MOCK_INCOMPLETE_BACKUP:-0}" -eq 0 ]]; then
		printf 'public' >"$target/20260813_site-files.tgz"
	fi
	printf 'private' >"$target/20260813_site-private-files.tgz"
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
	if [[ "${MOCK_FAIL_INSPECT:-0}" -eq 1 && "$2" == *"frontend"* ]]; then
		echo "Simulated inspect failure" >&2
		exit 1
	fi

	image="$(cat "$MOCK_STATE_DIR/current_image")"
	if [[ "$image" == 'leya/erpnext:stable' ]]; then
		image_id='sha256:stable-image-id'
	else
		image_id='sha256:target-image-id'
	fi
	format="${*: -1}"
	if [[ "$2" == *"-db-1" && "$format" == *".State.Health.Status"* ]]; then
		if [[ "${MOCK_FAIL_DATABASE:-0}" -eq 1 ]]; then
			echo 'true unhealthy'
		else
			echo 'true healthy'
		fi
	elif [[ "$2" == *"redis-cache"* || "$2" == *"redis-queue"* ]]; then
		echo 'true'
	elif [[ "$format" == *".Image"* && "$format" == *".RestartCount"* ]]; then
		restart_count=0
		if [[ "${MOCK_RESTART_COUNT:-0}" -eq 1 && "$2" == *"frontend"* && "$image" != 'leya/erpnext:stable' ]]; then
			restart_count=1
		fi
		if [[ "${MOCK_FAIL_IMAGE_ID:-0}" -eq 1 && "$2" == *"frontend"* && "$image" != 'leya/erpnext:stable' ]]; then
			image_id='sha256:wrong-image-id'
		fi
		echo "$image $image_id true $restart_count"
	elif [[ "$format" == *".Config.Image"* && "$format" == *".Image"* ]]; then
		echo "$image $image_id true"
	elif [[ "$format" == *".State.Running"* && "$format" == *".Config.Image"* ]]; then
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
	if [[ " $* " == *" backup --with-files --compress "* ]]; then
		if [[ "${MOCK_FAIL_BACKUP:-0}" -eq 1 ]]; then
			echo "Simulated backup failure" >&2
			exit 1
		fi
		exit 0
	fi

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

	if [[ " $* " == *" frappe.sessions.get_boot_assets_json "* ]]; then
		if [[ "${MOCK_FAIL_BOOT_MANIFEST:-0}" -eq 1 ]]; then
			echo '{"desk.bundle.js":"/assets/frappe/dist/js/desk.bundle.OLD.js"}'
		else
			cat "$MOCK_STATE_DIR/runtime-assets.json"
		fi
		exit 0
	fi

	if [[ "$*" == *" cat /home/frappe/frappe-bench/assets/assets.json"* ]]; then
		cat "$MOCK_STATE_DIR/assets.json"
		exit 0
	fi

	if [[ "$*" == *"assets/assets-rtl.json"* ]]; then
		cat "$MOCK_STATE_DIR/assets-rtl.json"
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

if [[ "$*" == *"/api/method/ping"* ]]; then
	current_image="$(cat "$MOCK_STATE_DIR/current_image")"
	if [[ "${MOCK_FAIL_PING:-0}" -eq 1 && "$current_image" == 'leya/erpnext:zh-verified' ]]; then
		printf '{"message":"not-pong"}'
	else
		printf '{"message":"pong"}'
	fi
elif [[ "$*" == *"/assets/assets.json"* ]]; then
	echo "curl_assets_match" >>"$MOCK_LOG_FILE"
	cat "$MOCK_STATE_DIR/assets.json"
elif [[ " $* " == *" -w "* ]]; then
	echo "curl_content_type_match" >>"$MOCK_LOG_FILE"
	if [[ "${MOCK_FAIL_MIME:-0}" -eq 1 ]]; then
		printf 'text/plain 64'
	elif [[ "$*" == *".css"* ]]; then
		printf 'text/css 64'
	else
		printf 'application/javascript 64'
	fi
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
{
  "desk.bundle.js": "/assets/frappe/dist/js/desk.bundle.TEST.js",
  "desk.bundle.css": "/assets/frappe/dist/css/desk.bundle.TEST.css"
}
EOF

cat >"$STATE_DIR/assets-rtl.json" <<'EOF'
{
  "desk.bundle.rtl.css": "/assets/frappe/dist/css/desk.bundle.rtl.TEST.css"
}
EOF

cat >"$STATE_DIR/runtime-assets.json" <<'EOF'
{
  "desk.bundle.js": "/assets/frappe/dist/js/desk.bundle.TEST.js",
  "desk.bundle.css": "/assets/frappe/dist/css/desk.bundle.TEST.css",
  "desk.bundle.rtl.css": "/assets/frappe/dist/css/desk.bundle.rtl.TEST.css"
}
EOF

run_deploy() {
	local scenario="$1"
	local output_file="$2"
	local path_value="$MOCK_BIN:$PATH"

	printf '%s' 'leya/erpnext:stable' >"$STATE_DIR/current_image"
	rm -f "$STATE_DIR/cache_cleared"
	rm -rf "$TEST_ROOT/backup"
	: >"$LOG_FILE"

	export PATH="$path_value"
	export MOCK_LOG_FILE="$LOG_FILE"
	export MOCK_STATE_DIR="$STATE_DIR"
	export MOCK_FAIL_RUNTIME=0
	export MOCK_FAIL_BOOT_MANIFEST=0
	export MOCK_FAIL_INSPECT=0
	export MOCK_FAIL_MIME=0
	export MOCK_FAIL_PING=0
	export MOCK_RESTART_COUNT=0
	export MOCK_FAIL_IMAGE_ID=0
	export MOCK_FAIL_BACKUP=0
	export MOCK_INCOMPLETE_BACKUP=0
	export MOCK_FAIL_DATABASE=0

	case "$scenario" in
		success) ;;
		runtime) export MOCK_FAIL_RUNTIME=1 ;;
		boot_manifest) export MOCK_FAIL_BOOT_MANIFEST=1 ;;
		inspect) export MOCK_FAIL_INSPECT=1 ;;
		mime) export MOCK_FAIL_MIME=1 ;;
		ping) export MOCK_FAIL_PING=1 ;;
		restart) export MOCK_RESTART_COUNT=1 ;;
		image_id) export MOCK_FAIL_IMAGE_ID=1 ;;
		backup) export MOCK_FAIL_BACKUP=1 ;;
		backup_incomplete) export MOCK_INCOMPLETE_BACKUP=1 ;;
		database) export MOCK_FAIL_DATABASE=1 ;;
		*) echo "Unknown scenario: $scenario" >&2; return 2 ;;
	esac
	export ZH_IMAGE='leya/erpnext:zh-verified'
	export SITE='erp.example.test'
	export PROJECT='leya-erpnext-v16'
	export PUBLIC_BASE_URL='https://erp.example.test'
	export WAIT_ATTEMPTS=1
	export WAIT_SECONDS=0
	export BACKUP_DIR="$TEST_ROOT/backup"

	bash "$SCRIPT_DIR/deploy_zh_cn_production.sh" >"$output_file" 2>&1
}

SUCCESS_OUTPUT="$TEST_ROOT/success.log"
run_deploy success "$SUCCESS_OUTPUT"

grep -F 'RUNTIME_ASSET_MANIFEST_OK assets=3' "$SUCCESS_OUTPUT" >/dev/null
grep -F 'PRODUCTION_BACKUP_VERIFIED' "$SUCCESS_OUTPUT" >/dev/null
grep -F 'ZH_CN_RUNTIME_TRANSLATIONS_OK' "$SUCCESS_OUTPUT" >/dev/null
grep -F 'PUBLIC_ASSET_VERIFICATION_OK assets=2' "$SUCCESS_OUTPUT" >/dev/null
grep -F 'ZH_CN_PRODUCTION_DEPLOYED_AND_VERIFIED image=leya/erpnext:zh-verified' "$SUCCESS_OUTPUT" >/dev/null
test "$(cat "$STATE_DIR/current_image")" = 'leya/erpnext:zh-verified'

CLEAR_LINE="$(grep -n '^cache_cleared$' "$LOG_FILE" | head -1 | cut -d: -f1)"
READ_LINE="$(grep -n '^runtime_translation_read$' "$LOG_FILE" | head -1 | cut -d: -f1)"
test "$CLEAR_LINE" -lt "$READ_LINE"

for scenario in runtime boot_manifest mime ping restart image_id; do
	FAILURE_OUTPUT="$TEST_ROOT/failure-$scenario.log"
	if run_deploy "$scenario" "$FAILURE_OUTPUT"; then
		echo "Expected simulated $scenario failure to fail deployment" >&2
		exit 1
	fi

	grep -F 'ZH_CN_DEPLOY_FAILED_ROLLING_BACK' "$FAILURE_OUTPUT" >/dev/null
	grep -F 'ROLLBACK_FINISHED image=leya/erpnext:stable' "$FAILURE_OUTPUT" >/dev/null
	test "$(cat "$STATE_DIR/current_image")" = 'leya/erpnext:stable'
done

INSPECT_OUTPUT="$TEST_ROOT/failure-inspect.log"
if run_deploy inspect "$INSPECT_OUTPUT"; then
	echo "Expected simulated inspect failure to fail deployment" >&2
	exit 1
fi
grep -F 'Simulated inspect failure' "$INSPECT_OUTPUT" >/dev/null
if grep -F 'ZH_CN_DEPLOY_FAILED_ROLLING_BACK' "$INSPECT_OUTPUT" >/dev/null; then
	echo "Pre-switch inspect failure must not invoke rollback" >&2
	exit 1
fi
test "$(cat "$STATE_DIR/current_image")" = 'leya/erpnext:stable'

BACKUP_OUTPUT="$TEST_ROOT/failure-backup.log"
if run_deploy backup "$BACKUP_OUTPUT"; then
	echo "Expected simulated backup failure to fail deployment" >&2
	exit 1
fi
grep -F 'Simulated backup failure' "$BACKUP_OUTPUT" >/dev/null
if grep -F 'ZH_CN_DEPLOY_FAILED_ROLLING_BACK' "$BACKUP_OUTPUT" >/dev/null; then
	echo "Pre-switch backup failure must not invoke rollback" >&2
	exit 1
fi
test "$(cat "$STATE_DIR/current_image")" = 'leya/erpnext:stable'

INCOMPLETE_BACKUP_OUTPUT="$TEST_ROOT/failure-backup-incomplete.log"
if run_deploy backup_incomplete "$INCOMPLETE_BACKUP_OUTPUT"; then
	echo "Expected incomplete backup to fail deployment" >&2
	exit 1
fi
grep -F "Current production backup is incomplete: ['public files']" "$INCOMPLETE_BACKUP_OUTPUT" >/dev/null
if grep -F 'ZH_CN_DEPLOY_FAILED_ROLLING_BACK' "$INCOMPLETE_BACKUP_OUTPUT" >/dev/null; then
	echo "Incomplete backup must fail before switching services" >&2
	exit 1
fi
test "$(cat "$STATE_DIR/current_image")" = 'leya/erpnext:stable'

DATABASE_OUTPUT="$TEST_ROOT/failure-database.log"
if run_deploy database "$DATABASE_OUTPUT"; then
	echo "Expected unhealthy database to fail deployment" >&2
	exit 1
fi
grep -F 'Production database is not healthy: state=true unhealthy' "$DATABASE_OUTPUT" >/dev/null
if grep -F 'ZH_CN_DEPLOY_FAILED_ROLLING_BACK' "$DATABASE_OUTPUT" >/dev/null; then
	echo "Database preflight failure must not invoke rollback" >&2
	exit 1
fi
test "$(cat "$STATE_DIR/current_image")" = 'leya/erpnext:stable'

echo DEPLOY_ZH_CN_PRODUCTION_TEST_OK
