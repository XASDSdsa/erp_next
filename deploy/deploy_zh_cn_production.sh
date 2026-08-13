#!/usr/bin/env bash

set -Eeuo pipefail

: "${ZH_IMAGE:?Set ZH_IMAGE to the verified Simplified Chinese image tag}"
export ZH_IMAGE

SITE="${SITE:-erp-sunny.leyabilliards.com}"
PROJECT="${PROJECT:-leya-erpnext-v16}"
BASE_COMPOSE="${BASE_COMPOSE:-compose.production.yaml}"
OVERRIDE="${OVERRIDE:-build/zh-cn/compose.zh-cn.yaml}"
SITES_VOLUME="${SITES_VOLUME:-frappe_docker_sites}"
BACKEND="${BACKEND:-${PROJECT}-backend-1}"
PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-https://${SITE}}"
WAIT_ATTEMPTS="${WAIT_ATTEMPTS:-60}"
WAIT_SECONDS="${WAIT_SECONDS:-2}"
BACKUP_DIR="${BACKUP_DIR:-build/production-backups/$(date +%Y%m%d-%H%M%S)}"

SERVICES=(backend websocket frontend queue-long queue-short scheduler)
EXPECTED_TRANSLATIONS=(
	"Brand=品牌"
	"List View=列表视图"
	"Saved Filters=已保存的筛选器"
	"Getting Started=入门指南"
	"Create Warehouses=创建仓库"
	"Create Item=创建物料"
	"Create Purchase Receipt=创建采购入库单"
	"Create Transfer Entry=创建库存调拨单"
	"View Stock Balance=查看库存余额"
	"Review Stock Settings=查看库存设置"
)

RUNTIME_JSON="$(mktemp)"
INTERNAL_MANIFEST="$(mktemp)"
INTERNAL_RTL_MANIFEST="$(mktemp)"
BOOT_MANIFEST="$(mktemp)"
PUBLIC_MANIFEST="$(mktemp)"
PREVIOUS_IMAGE=""
PREVIOUS_IMAGE_ID=""
TARGET_IMAGE_ID=""
MAINTENANCE_ENABLED=0
SERVICES_SWITCHED=0

cleanup_files() {
	rm -f \
		"$RUNTIME_JSON" \
		"$INTERNAL_MANIFEST" \
		"$INTERNAL_RTL_MANIFEST" \
		"$BOOT_MANIFEST" \
		"$PUBLIC_MANIFEST"
}

wait_for_backend() {
	for attempt in $(seq 1 "$WAIT_ATTEMPTS"); do
		if docker exec "$BACKEND" bench --site "$SITE" list-apps >/dev/null 2>&1; then
			return 0
		fi
		echo "backend_wait attempt=$attempt"
		sleep "$WAIT_SECONDS"
	done

	return 1
}

wait_for_https() {
	local response

	for attempt in $(seq 1 "$WAIT_ATTEMPTS"); do
		if response="$(curl --fail --silent --show-error --max-time 10 \
			"${PUBLIC_BASE_URL}/api/method/ping")"; then
			if PING_RESPONSE="$response" python3 -c '
import json
import os

response = json.loads(os.environ["PING_RESPONSE"])
assert response == {"message": "pong"}, response
'; then
				return 0
			fi
		fi
		echo "https_wait attempt=$attempt"
		sleep "$WAIT_SECONDS"
	done

	return 1
}

clear_translation_cache() {
	docker exec "$BACKEND" \
		bench --site "$SITE" execute frappe.translate.clear_cache >/dev/null
	docker exec "$BACKEND" bench --site "$SITE" clear-cache
	docker exec "$BACKEND" bench --site "$SITE" clear-website-cache
}

create_verified_backup() {
	local backup_started_at
	backup_started_at="$(date +%s)"

	install -d -m 0700 "$BACKUP_DIR"
	docker exec "$BACKEND" \
		bench --site "$SITE" backup --with-files --compress
	docker cp \
		"$BACKEND:/home/frappe/frappe-bench/sites/$SITE/private/backups/." \
		"$BACKUP_DIR/"
	chmod -R go-rwx "$BACKUP_DIR"

	BACKUP_DIR="$BACKUP_DIR" BACKUP_STARTED_AT="$backup_started_at" python3 -c '
import os
from pathlib import Path

backup_dir = Path(os.environ["BACKUP_DIR"])
started_at = int(os.environ["BACKUP_STARTED_AT"])
files = [
    path
    for path in backup_dir.iterdir()
    if path.is_file() and path.stat().st_size > 0 and path.stat().st_mtime >= started_at - 2
]
checks = {
    "site configuration": lambda name: name.endswith("-site_config_backup.json"),
    "database": lambda name: name.endswith("-database.sql.gz"),
    "public files": lambda name: name.endswith("-files.tgz") and not name.endswith("-private-files.tgz"),
    "private files": lambda name: name.endswith("-private-files.tgz"),
}
missing = [label for label, matches in checks.items() if not any(matches(path.name) for path in files)]
assert not missing, f"Current production backup is incomplete: {missing}"
print(f"PRODUCTION_BACKUP_VERIFIED path={backup_dir} files={len(files)}")
'
}

set_maintenance_off() {
	if docker exec "$BACKEND" bench --site "$SITE" set-maintenance-mode off; then
		MAINTENANCE_ENABLED=0
		return 0
	fi

	return 1
}

rollback() {
	local exit_code="$1"
	local rollback_ok=1
	trap - EXIT INT TERM
	set +e

	echo "ZH_CN_DEPLOY_FAILED_ROLLING_BACK"

	if [[ "$SERVICES_SWITCHED" -eq 1 && -n "$PREVIOUS_IMAGE" ]]; then
		if docker exec "$BACKEND" bench --site "$SITE" set-maintenance-mode on; then
			MAINTENANCE_ENABLED=1
		else
			rollback_ok=0
		fi

		ZH_IMAGE="$PREVIOUS_IMAGE" docker compose \
			-p "$PROJECT" \
			-f "$BASE_COMPOSE" \
			-f "$OVERRIDE" \
			up -d \
			--no-deps \
			--force-recreate \
			--pull never \
			"${SERVICES[@]}" || rollback_ok=0
		wait_for_backend || rollback_ok=0
		clear_translation_cache || rollback_ok=0

		for service in "${SERVICES[@]}"; do
			container="${PROJECT}-${service}-1"
			state="$(docker inspect "$container" --format '{{.Config.Image}} {{.Image}} {{.State.Running}} {{.RestartCount}}')"
			if [[ "$state" != "$PREVIOUS_IMAGE $PREVIOUS_IMAGE_ID true 0" ]]; then
				echo "Rollback container mismatch: $container state=$state" >&2
				rollback_ok=0
			fi
		done
	fi

	if [[ "$MAINTENANCE_ENABLED" -eq 1 ]]; then
		set_maintenance_off || rollback_ok=0
	fi

	wait_for_https || rollback_ok=0
	cleanup_files

	if [[ "$rollback_ok" -eq 1 ]]; then
		echo "ROLLBACK_FINISHED image=${PREVIOUS_IMAGE:-unchanged}"
	else
		echo "ROLLBACK_INCOMPLETE_MANUAL_CHECK_REQUIRED image=${PREVIOUS_IMAGE:-unknown}" >&2
	fi

	exit "$exit_code"
}

finish() {
	local exit_code=$?

	if [[ "$exit_code" -ne 0 && ( "$SERVICES_SWITCHED" -eq 1 || "$MAINTENANCE_ENABLED" -eq 1 ) ]]; then
		rollback "$exit_code"
	fi

	cleanup_files
}

trap finish EXIT
trap 'exit 130' INT TERM

TARGET_IMAGE_ID="$(docker image inspect "$ZH_IMAGE" --format '{{.Id}}')"
if [[ -z "$TARGET_IMAGE_ID" ]]; then
	echo "Unable to determine target image ID: $ZH_IMAGE" >&2
	exit 1
fi

docker run --rm \
	--entrypoint /home/frappe/frappe-bench/env/bin/python \
	"$ZH_IMAGE" \
	apps/erpnext/deploy/verify_zh_cn_image.py

docker run --rm \
	-v "$SITES_VOLUME":/home/frappe/frappe-bench/sites:ro \
	--entrypoint /home/frappe/frappe-bench/env/bin/python \
	"$ZH_IMAGE" \
	apps/erpnext/deploy/verify_zh_cn_image.py

ZH_IMAGE="$ZH_IMAGE" docker compose \
	-p "$PROJECT" \
	-f "$BASE_COMPOSE" \
	-f "$OVERRIDE" \
	config --format json |
	python3 -c '
import json
import os
import sys

config = json.load(sys.stdin)
expected = os.environ["ZH_IMAGE"]
services = ["backend", "websocket", "frontend", "queue-long", "queue-short", "scheduler"]

for service in services:
    actual = config["services"][service]["image"]
    print(f"{service}={actual}")
    assert actual == expected, (service, actual, expected)

print("ZH_CN_COMPOSE_PREFLIGHT_OK")
'

database_state="$(docker inspect "${PROJECT}-db-1" --format '{{.State.Running}} {{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}')"
if [[ "$database_state" != "true healthy" ]]; then
	echo "Production database is not healthy: state=$database_state" >&2
	exit 1
fi

for service in redis-cache redis-queue; do
	running="$(docker inspect "${PROJECT}-${service}-1" --format '{{.State.Running}}')"
	if [[ "$running" != "true" ]]; then
		echo "Production dependency is not running: service=$service state=$running" >&2
		exit 1
	fi
done

for service in "${SERVICES[@]}"; do
	state="$(docker inspect "${PROJECT}-${service}-1" --format '{{.Config.Image}} {{.Image}} {{.State.Running}}')"
	read -r image image_id running <<<"$state"
	if [[ -z "$image" || -z "$image_id" || "$running" != "true" ]]; then
		echo "Invalid production container state: service=$service state=$state" >&2
		exit 1
	fi

	if [[ -z "$PREVIOUS_IMAGE" ]]; then
		PREVIOUS_IMAGE="$image"
		PREVIOUS_IMAGE_ID="$image_id"
	elif [[ "$image" != "$PREVIOUS_IMAGE" || "$image_id" != "$PREVIOUS_IMAGE_ID" ]]; then
		echo "Production application services do not share one image:" >&2
		echo "  expected=$PREVIOUS_IMAGE $PREVIOUS_IMAGE_ID" >&2
		echo "  service=$service image=$image image_id=$image_id" >&2
		exit 1
	fi
done

echo "previous_image=$PREVIOUS_IMAGE"
echo "previous_image_id=$PREVIOUS_IMAGE_ID"
echo "target_image=$ZH_IMAGE"
echo "target_image_id=$TARGET_IMAGE_ID"

create_verified_backup

MAINTENANCE_ENABLED=1
docker exec "$BACKEND" bench --site "$SITE" set-maintenance-mode on

SERVICES_SWITCHED=1
ZH_IMAGE="$ZH_IMAGE" docker compose \
	-p "$PROJECT" \
	-f "$BASE_COMPOSE" \
	-f "$OVERRIDE" \
	up -d \
	--no-deps \
	--force-recreate \
	--pull never \
	"${SERVICES[@]}"

wait_for_backend

for service in "${SERVICES[@]}"; do
	container="${PROJECT}-${service}-1"
	state="$(docker inspect "$container" --format '{{.Config.Image}} {{.Image}} {{.State.Running}} {{.RestartCount}}')"
	echo "container=$container state=$state"
	if [[ "$state" != "$ZH_IMAGE $TARGET_IMAGE_ID true 0" ]]; then
		echo "Application container failed health check: $container state=$state" >&2
		exit 1
	fi
done

docker exec "$BACKEND" \
	/home/frappe/frappe-bench/env/bin/python \
	/home/frappe/frappe-bench/apps/erpnext/deploy/verify_zh_cn_image.py

# Translation dictionaries are cached in Redis. Clear them only after the new
# backend is running, and before checking the runtime translation dictionary.
clear_translation_cache

docker exec "$BACKEND" \
	bench --site "$SITE" execute frappe.sessions.get_boot_assets_json \
	>"$BOOT_MANIFEST"

docker exec "$BACKEND" cat \
	/home/frappe/frappe-bench/assets/assets.json >"$INTERNAL_MANIFEST"

docker exec "$BACKEND" sh -lc '
if [ -f /home/frappe/frappe-bench/assets/assets-rtl.json ]; then
    cat /home/frappe/frappe-bench/assets/assets-rtl.json
else
    printf "{}"
fi
' >"$INTERNAL_RTL_MANIFEST"

export BOOT_MANIFEST INTERNAL_MANIFEST INTERNAL_RTL_MANIFEST
python3 -c '
import json
import os

with open(os.environ["BOOT_MANIFEST"], encoding="utf-8") as handle:
    boot = json.load(handle)
with open(os.environ["INTERNAL_MANIFEST"], encoding="utf-8") as handle:
    internal = json.load(handle)
with open(os.environ["INTERNAL_RTL_MANIFEST"], encoding="utf-8") as handle:
    internal_rtl = json.load(handle)

assert isinstance(boot, dict), type(boot)
assert isinstance(internal, dict), type(internal)
assert isinstance(internal_rtl, dict), type(internal_rtl)

expected = dict(internal)
expected.update(internal_rtl)
assert boot == expected, "Frappe runtime assets cache does not match the running image"
print(f"RUNTIME_ASSET_MANIFEST_OK assets={len(boot)}")
'

docker exec "$BACKEND" \
	bench --site "$SITE" execute frappe.translate.get_all_translations \
	--args '["zh"]' >"$RUNTIME_JSON"

EXPECTED_TRANSLATIONS_JSON="$(printf '%s\n' "${EXPECTED_TRANSLATIONS[@]}" | python3 -c '
import json
import sys

translations = {}
for line in sys.stdin:
    key, value = line.rstrip("\n").split("=", 1)
    translations[key] = value
print(json.dumps(translations, ensure_ascii=False))
')"
export EXPECTED_TRANSLATIONS_JSON RUNTIME_JSON

python3 -c '
import json
import os

with open(os.environ["RUNTIME_JSON"], encoding="utf-8") as handle:
    actual = json.load(handle)
expected = json.loads(os.environ["EXPECTED_TRANSLATIONS_JSON"])

for key, wanted in expected.items():
    received = actual.get(key)
    print(f"{key} => {received}")
    assert received == wanted, (key, received, wanted)

print("ZH_CN_RUNTIME_TRANSLATIONS_OK")
'

set_maintenance_off
wait_for_https

DEPLOY_ID="$(date +%s)"
curl --fail --silent --show-error --location --max-time 15 \
	-H 'Cache-Control: no-cache' \
	"${PUBLIC_BASE_URL}/assets/assets.json?deploy=${DEPLOY_ID}" >"$PUBLIC_MANIFEST"

export INTERNAL_MANIFEST PUBLIC_MANIFEST
python3 -c '
import json
import os

with open(os.environ["INTERNAL_MANIFEST"], encoding="utf-8") as handle:
    internal = json.load(handle)
with open(os.environ["PUBLIC_MANIFEST"], encoding="utf-8") as handle:
    public = json.load(handle)

assert public == internal, "Public assets.json does not match the running image"
print(f"PUBLIC_ASSET_MANIFEST_OK assets={len(public)}")
'

while IFS= read -r asset_path; do
	asset_response="$(
		curl --fail --silent --show-error --location --max-time 15 \
			-o /dev/null \
			-w '%{content_type} %{size_download}' \
			"${PUBLIC_BASE_URL}${asset_path}?deploy=${DEPLOY_ID}"
	)"
	content_type="${asset_response% *}"
	size_download="${asset_response##* }"
	media_type="${content_type%%;*}"

	case "$asset_path" in
		*.js)
			if [[ "$media_type" != "application/javascript" && \
				"$media_type" != "text/javascript" && \
				"$media_type" != "application/x-javascript" ]]; then
				echo "Invalid JavaScript MIME type: $asset_path content_type=$content_type" >&2
				exit 1
			fi
			;;
		*.css)
			if [[ "$media_type" != "text/css" ]]; then
				echo "Invalid CSS MIME type: $asset_path content_type=$content_type" >&2
				exit 1
			fi
			;;
		*)
			echo "Unsupported asset extension in manifest: $asset_path" >&2
			exit 1
			;;
	esac

	if [[ ! "$size_download" =~ ^[0-9]+$ || "$size_download" -eq 0 ]]; then
		echo "Empty public asset response: $asset_path size=$size_download" >&2
		exit 1
	fi
done < <(
	python3 -c '
import json
import os

with open(os.environ["PUBLIC_MANIFEST"], encoding="utf-8") as handle:
    manifest = json.load(handle)
for path in sorted(set(manifest.values())):
    print(path)
'
)

echo "PUBLIC_ASSET_VERIFICATION_OK assets=$(python3 -c 'import json, os; print(len(json.load(open(os.environ["PUBLIC_MANIFEST"]))))')"

cleanup_files
echo "ZH_CN_PRODUCTION_DEPLOYED_AND_VERIFIED image=$ZH_IMAGE"
