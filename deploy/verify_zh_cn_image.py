from __future__ import annotations

import json
from gettext import GNUTranslations
from pathlib import Path

from babel.messages.pofile import read_po


BENCH_ROOT = Path("/home/frappe/frappe-bench")
ASSETS_ROOT = BENCH_ROOT / "sites" / "assets"
IMAGE_ASSETS_ROOT = BENCH_ROOT / "assets"

EXPECTED_TRANSLATIONS = {
	"Brand": "品牌",
	"List View": "列表视图",
	"Saved Filters": "已保存的筛选器",
	"Getting Started": "入门指南",
	"Create Warehouses": "创建仓库",
	"Create Item": "创建物料",
	"Create Purchase Receipt": "创建采购入库单",
	"Create Transfer Entry": "创建库存调拨单",
	"View Stock Balance": "查看库存余额",
	"Review Stock Settings": "查看库存设置",
}

REQUIRED_BUNDLES = {
	"desk.bundle.js",
	"list.bundle.js",
	"form.bundle.js",
	"report.bundle.js",
	"erpnext.bundle.js",
	"desk.bundle.css",
	"erpnext.bundle.css",
}


def load_manifest(filename: str, *, required: bool = True) -> dict[str, str]:
	manifest_path = ASSETS_ROOT / filename
	if not manifest_path.exists() and not required:
		return {}

	manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
	if not isinstance(manifest, dict):
		raise AssertionError(f"Invalid asset manifest: {manifest_path}")
	return manifest


def verify_asset_layout() -> None:
	if not ASSETS_ROOT.is_symlink():
		raise AssertionError(f"Expected asset symlink: {ASSETS_ROOT}")

	resolved_assets_root = ASSETS_ROOT.resolve(strict=True)
	if resolved_assets_root != IMAGE_ASSETS_ROOT:
		raise AssertionError(
			f"Asset symlink resolves to {resolved_assets_root}, expected {IMAGE_ASSETS_ROOT}"
		)


def verify_assets(manifest: dict[str, str], manifest_name: str) -> None:
	missing_files = []
	for bundle, public_path in manifest.items():
		if not isinstance(public_path, str) or not public_path.startswith("/assets/"):
			raise AssertionError(f"Invalid asset path for {bundle}: {public_path!r}")

		relative_path = Path(public_path.removeprefix("/assets/"))
		if relative_path.is_absolute() or ".." in relative_path.parts:
			raise AssertionError(f"Unsafe asset path for {bundle}: {public_path!r}")

		asset_path = ASSETS_ROOT / relative_path
		if not asset_path.is_file() or asset_path.stat().st_size == 0:
			missing_files.append(f"{bundle} -> {asset_path}")

	if missing_files:
		raise AssertionError(
			f"Asset files from {manifest_name} are missing or empty:\n"
			+ "\n".join(missing_files)
		)


def load_translations(app: str) -> GNUTranslations:
	mo_path = ASSETS_ROOT / "locale" / "zh" / "LC_MESSAGES" / f"{app}.mo"
	with mo_path.open("rb") as mo_file:
		return GNUTranslations(mo_file)


def verify_translations() -> None:
	catalogs = [load_translations("frappe"), load_translations("erpnext")]

	mismatches = {}
	for message, expected in EXPECTED_TRANSLATIONS.items():
		actual = next(
			(translation for catalog in catalogs if (translation := catalog.gettext(message)) != message),
			None,
		)
		if actual != expected:
			mismatches[message] = {"expected": expected, "actual": actual}
	if mismatches:
		raise AssertionError(
			"Simplified Chinese translation mismatch:\n"
			+ json.dumps(mismatches, ensure_ascii=False, indent=2)
		)


def verify_translation_coverage() -> int:
	message_count = 0
	problems = []

	for app in ("frappe", "erpnext"):
		po_path = BENCH_ROOT / "apps" / app / app / "locale" / "zh.po"
		with po_path.open("rb") as po_file:
			catalog = read_po(po_file)

		for message in catalog:
			if not message.id:
				continue

			message_count += 1
			if "fuzzy" in message.flags:
				problems.append(f"{app}: fuzzy: {message.id!r}")
				continue

			translation = message.string
			if isinstance(translation, tuple):
				missing = not translation or any(not value for value in translation)
			else:
				missing = not translation

			if missing:
				problems.append(f"{app}: untranslated: {message.id!r}")

	if problems:
		details = "\n".join(problems[:50])
		if len(problems) > 50:
			details += f"\n... and {len(problems) - 50} more"
		raise AssertionError(f"Simplified Chinese PO coverage failed:\n{details}")

	return message_count


def main() -> None:
	verify_asset_layout()
	manifest = load_manifest("assets.json")
	rtl_manifest = load_manifest("assets-rtl.json", required=False)
	missing_bundles = sorted(REQUIRED_BUNDLES - manifest.keys())
	if missing_bundles:
		raise AssertionError(f"Required bundles missing from assets.json: {missing_bundles}")
	verify_assets(manifest, "assets.json")
	verify_assets(rtl_manifest, "assets-rtl.json")
	verify_translations()
	message_count = verify_translation_coverage()
	print(
		"ZH_CN_IMAGE_VERIFICATION_OK "
		f"assets={len(manifest)} rtl_assets={len(rtl_manifest)} "
		f"sampled_translations={len(EXPECTED_TRANSLATIONS)} "
		f"translated_messages={message_count}"
	)


if __name__ == "__main__":
	main()
