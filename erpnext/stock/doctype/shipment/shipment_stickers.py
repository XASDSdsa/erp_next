"""Read sticker pictures explicitly present on a Delivery Note, without guessing."""

from urllib.parse import unquote, urlsplit

import frappe
from frappe.utils import flt


STICKER_TEMPLATE = "巧克粉贴纸"


def _local_image_url(value):
	"""Accept existing site attachments only; never retrieve external pictures."""
	if not isinstance(value, str) or not value:
		return None
	try:
		parsed = urlsplit(value)
	except ValueError:
		return None
	path = unquote(parsed.path)
	if (
		parsed.scheme or parsed.netloc or parsed.query or parsed.fragment
		or not path.startswith(("/files/", "/private/files/"))
		or not path.rsplit("/", 1)[-1]
		or any(part in {".", ".."} for part in path.split("/"))
		or any(ord(char) < 32 or char in "\\<>" for char in path)
	):
		return None
	return path


def _read_item(code):
	result = {
		"item_code": code, "item_name": "", "model": "", "version": "",
		"image_url": None, "image_status": "unavailable",
	}
	try:
		item = frappe.get_doc("Item", code)
	except frappe.DoesNotExistError:
		return result, None
	if not item.has_permission("read"):
		result["image_status"] = "restricted"
		return result, None
	# A variant may have changed since classification. Never substitute another item.
	if item.get("variant_of") != STICKER_TEMPLATE:
		return None, None
	attributes = {row.get("attribute"): row.get("attribute_value") for row in item.get("attributes") or []}
	result.update(
		item_name=item.get("item_name") or code,
		model=attributes.get("贴纸型号") or "",
		version=attributes.get("贴纸版本") or "",
	)
	if not item.get("image"):
		result["image_status"] = "missing"
		return result, None
	return result, _local_image_url(item.image)


def _read_image(path, names):
	"""Same URL can have multiple File copies with different attachment permissions."""
	restricted = False
	for name in names:
		try:
			file = frappe.get_doc("File", name)
		except frappe.DoesNotExistError:
			continue
		if not file.has_permission("read"):
			restricted = True
			continue
		if (
			file.get("is_folder")
			or _local_image_url(file.get("file_url")) != path
			or bool(file.get("is_private")) != path.startswith("/private/files/")
		):
			continue
		# Existing thumbnails can be public derivatives of private files. Using the
		# permission-checked original avoids exposing a less restricted derivative.
		return {"image_status": "ready", "image_url": path}
	return {"image_status": "restricted" if restricted else "unavailable", "image_url": None}


def stickers_for_delivery_note(dn):
	"""Map DN Item row names to permission-checked, explicitly linked sticker data.

	Direct sticker items and native packed components are supported. Components
	need parent_detail_docname; matching by customer, item name or BOM is unsafe
	when the same product occurs on multiple rows. Negative return quantities
	remain inspectable; zero-quantity rows/components have no physical sticker.
	"""
	dn.check_permission("read")
	rows = {
		row.name: row for row in dn.get("items") or []
		if row.get("name") and row.get("item_code") and flt(row.get("qty"))
	}
	links = [(name, row.item_code, "item") for name, row in rows.items()]
	for component in dn.get("packed_items") or []:
		parent = component.get("parent_detail_docname")
		if parent not in rows or not component.get("item_code") or not flt(component.get("qty")):
			continue
		# Reject stale or inconsistent row links rather than attach to another product.
		if component.get("parent_item") and component.parent_item != rows[parent].item_code:
			continue
		links.append((parent, component.item_code, "packed_item"))
	if not links:
		return {}
	codes = list(dict.fromkeys(code for _, code, _ in links))
	# Classification is the only lookup needed for ordinary goods. These names
	# already originate in a readable DN; no Item metadata/URL is exposed here.
	sticker_codes = set(frappe.get_all(
		"Item", filters={"name": ["in", codes], "variant_of": STICKER_TEMPLATE}, pluck="name"
	))
	if not sticker_codes:
		return {}
	items, image_paths = {}, {}
	for code in codes:
		if code not in sticker_codes:
			continue
		result, path = _read_item(code)
		if result is not None:
			items[code] = result
			if path:
				image_paths[code] = path
	if image_paths:
		files_by_path = {}
		for row in frappe.get_all(
			"File", filters={"file_url": ["in", list(set(image_paths.values()))]},
			fields=["name", "file_url"], order_by="creation asc",
		):
			files_by_path.setdefault(row.file_url, []).append(row.name)
		images = {
			path: _read_image(path, files_by_path.get(path, []))
			for path in dict.fromkeys(image_paths.values())
		}
		for code, path in image_paths.items():
			items[code].update(images[path])
	result, seen = {}, set()
	for row_name, code, source_kind in links:
		if code not in items or (row_name, code) in seen:
			continue
		seen.add((row_name, code))
		result.setdefault(row_name, []).append({**items[code], "source_kind": source_kind})
	return result

