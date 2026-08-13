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
PUBLIC_MANIFEST="$(mktemp)"
PREVIOUS_IMAGE=""
MAINTENANCE_ENABLED=0
SERVICES_SWITCHED=0

cleanup_files() {
	rm -f "$RUNTIME_JSON" "$INTERNAL_MANIFEST" "$PUBLIC_MANIFEST"
}

wait_for_backend() {
	for attempt in $(seq 1 60); do
		if docker exec "$BACKEND" bench --site "$SITE" list-apps >/dev/null 2>&1; then
			return 0
		fi
		echo "backend_wait attempt=$attempt"
		sleep 2
	done

	return 1
}

wait_for_https() {
	for attempt in $(seq 1 60); do
		if curl --fail --silent --show-error --max-time 10 \
			"${PUBLIC_BASE_URL}/api/method/ping" >/dev/null; then
			return 0
		fi
		echo "https_wait attempt=$attempt"
		sleep 2
	done

	return 1
}

clear_translation_cache() {
	docker exec "$BACKEND" \
		bench --site "$SITE" execute frappe.translate.clear_cache >/dev/null
	docker exec "$BACKEND" bench --site "$SITE" clear-cache
	docker exec "$BACKEND" bench --site "$SITE" clear-website-cache
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
			state="$(docker inspect "$container" --format '{{.Config.Image}} {{.State.Running}}')"
			if [[ "$state" != "$PREVIOUS_IMAGE true" ]]; then
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

docker image inspect "$ZH_IMAGE" >/dev/null

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

RUNNING_IMAGES=()
while IFS= read -r image; do
	RUNNING_IMAGES+=("$image")
done < <(
	for service in "${SERVICES[@]}"; do
		docker inspect "${PROJECT}-${service}-1" --format '{{.Config.Image}}'
	done | sort -u
)

if [[ "${#RUNNING_IMAGES[@]}" -ne 1 ]]; then
	echo "Production application services do not share one image:" >&2
	printf '  %s\n' "${RUNNING_IMAGES[@]}" >&2
	exit 1
fi

PREVIOUS_IMAGE="${RUNNING_IMAGES[0]}"
echo "previous_image=$PREVIOUS_IMAGE"
echo "target_image=$ZH_IMAGE"

docker exec "$BACKEND" bench --site "$SITE" set-maintenance-mode on
MAINTENANCE_ENABLED=1

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
	docker inspect "$container" --format \
		'container={{.Name}} image={{.Config.Image}} running={{.State.Running}} restart_count={{.RestartCount}}' \
		| grep -F "image=$ZH_IMAGE running=true"
done

docker exec "$BACKEND" \
	/home/frappe/frappe-bench/env/bin/python \
	/home/frappe/frappe-bench/apps/erpnext/deploy/verify_zh_cn_image.py

# Translation dictionaries are cached in Redis. Clear them only after the new
# backend is running, and before checking the runtime translation dictionary.
clear_translation_cache

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

docker exec "$BACKEND" cat \
	/home/frappe/frappe-bench/assets/assets.json >"$INTERNAL_MANIFEST"

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
	content_type="$(
		curl --fail --silent --show-error --location --max-time 15 \
			-o /dev/null \
			-w '%{content_type}' \
			"${PUBLIC_BASE_URL}${asset_path}?deploy=${DEPLOY_ID}"
	)"

	if [[ -z "$content_type" || "$content_type" == text/html* ]]; then
		echo "Invalid public asset response: $asset_path content_type=$content_type" >&2
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
