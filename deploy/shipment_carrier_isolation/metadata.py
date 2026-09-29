#!/usr/bin/env python3
"""Bounded deployment metadata operations; run with the image's Frappe Python.

No business records are written. Restore bypasses document hooks and never drops
business columns. A changed business table/schema blocks automatic restoration.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlparse

import frappe
from frappe.utils.response import json_handler

DOCTYPES = ["Shipment", "Shipment Parcel", "Delivery Note"]
BUSINESS = DOCTYPES + [
    "Shipment Delivery Note", "Delivery Note Item", "Sales Order", "Sales Order Item",
    "SF Waybill", "GL Entry", "Stock Ledger Entry", "Payment Entry", "Journal Entry",
]
PATCH = "erpnext.patches.v16_0.move_shipment_carrier_metadata.execute"
SF_SYNC = "sf_international.install.ensure_sf_shipment_metadata"


def encoded(value):
    return json.dumps(value, default=json_handler, sort_keys=True, ensure_ascii=False).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def business():
    result = {}
    for doctype in BUSINESS:
        if frappe.db.table_exists(doctype):
            rows = frappe.get_all(doctype, fields=["*"], order_by="name")
            schema = frappe.db.sql("SHOW CREATE TABLE `tab" + doctype + "`")[0][1]
            result[doctype] = {"count": len(rows), "rows_sha256": digest(rows), "schema_sha256": digest(schema)}
    return result


def scopes():
    result = [{"doctype": "DocType", "filters": {"name": ["in", DOCTYPES]}}]
    for field in frappe.get_meta("DocType").get_table_fields():
        result.append({"doctype": field.options, "filters": {"parent": ["in", DOCTYPES], "parenttype": "DocType"}})
    result.extend([
        {"doctype": "Custom Field", "filters": {"dt": ["in", DOCTYPES]}},
        {"doctype": "Property Setter", "filters": {"doc_type": ["in", DOCTYPES]}},
        {"doctype": "Custom DocPerm", "filters": {"parent": ["in", DOCTYPES]}},
        {"doctype": "DocType Layout", "filters": {"document_type": ["in", DOCTYPES]}},
        {"doctype": "List View Settings", "filters": {"name": "Sales Order"}},
        {"doctype": "Client Script", "filters": {"name": ["in", ["Sales Order Payment List", "Sales Order Freight List"]], "dt": "Sales Order", "view": "List"}},
    ])
    layouts = frappe.get_all("DocType Layout", filters={"document_type": ["in", DOCTYPES]}, pluck="name")
    if layouts:
        for field in frappe.get_meta("DocType Layout").get_table_fields():
            result.append({"doctype": field.options, "filters": {"parent": ["in", layouts], "parenttype": "DocType Layout"}})
    return result


def capture():
    groups = []
    for scope in scopes():
        groups.append({**scope, "rows": frappe.get_all(scope["doctype"], filters=scope["filters"], fields=["*"], order_by="name")})
    return {"site": frappe.local.site, "metadata": groups, "business": business()}


def semantic(snapshot):
    groups = []
    for group in snapshot["metadata"]:
        rows = [{k: v for k, v in row.items() if k not in {"name", "creation", "modified", "modified_by"}} for row in group["rows"]]
        groups.append({**group, "rows": sorted(rows, key=lambda row: encoded(row))})
    return digest(groups)


def validate(snapshot):
    assert business() == snapshot["business"], "business_rows_or_schema_changed"
    meta = frappe.get_meta("Shipment", cached=False)
    manual = [field for field in meta.fields if field.fieldname.startswith("manual_")]
    assert len(manual) == 19 and len({field.fieldname for field in manual}) == 19, "manual_field_count"
    assert not any(field.get("is_custom_field") for field in manual), "manual_field_still_custom"
    assert meta.get_field("shipment_contents").fieldtype == "HTML"
    provider = meta.get_field("service_provider")
    assert provider.fieldtype == "Select"
    assert "其他物流（手工登记）" in provider.options.splitlines() and "顺丰国际" in provider.options.splitlines()
    assert meta.get_field("column_break_28").fieldtype == "Section Break"
    dn = frappe.get_meta("Delivery Note", cached=False)
    assert dn.get_field("shipping_details").fieldtype == "HTML"
    assert dn.get_field("shipping_section").fieldtype == "Section Break"
    assert not dn.get_field("sf_dn_actions_section") and not dn.get_field("sf_dn_actions_html")
    sales = [permission for permission in meta.permissions if permission.role == "Sales User"]
    assert sales and any(permission.read and permission.create and permission.submit for permission in sales), "sales_user_permissions"
    assert frappe.db.sql("SHOW INDEX FROM `tabShipment` WHERE Key_name='manual_carrier_waybill_unique' AND Non_unique=0"), "manual_unique_index"
    for doctype, key in (("Shipment", "shipment_contents"), ("Delivery Note", "shipping_state")):
        names = frappe.get_all(doctype, filters={"docstatus": ["!=", 2]}, pluck="name", limit=1)
        if names:
            doc = frappe.get_doc(doctype, names[0])
            doc.run_method("onload")
            assert key in (doc.get("__onload") or {}), "missing_native_onload:" + doctype
    assert business() == snapshot["business"], "onload_changed_business_rows"
    print("METADATA_AND_BUSINESS_VALIDATED")


def restore(snapshot):
    assert snapshot["site"] == frappe.local.site, "snapshot_site_mismatch"
    assert business() == snapshot["business"], "rollback_refused_business_or_schema_changed"
    current = {(scope["doctype"], encoded(scope["filters"])) for scope in scopes()}
    expected = {(group["doctype"], encoded(group["filters"])) for group in snapshot["metadata"]}
    assert current == expected, "rollback_metadata_scope_changed"
    for group in reversed(snapshot["metadata"]):
        frappe.db.delete(group["doctype"], group["filters"])
    for group in snapshot["metadata"]:
        if not group["rows"]:
            continue
        fields = list(group["rows"][0])
        frappe.db.bulk_insert(group["doctype"], fields, [tuple(row[field] for field in fields) for row in group["rows"]])
    frappe.db.commit()
    frappe.clear_cache()
    assert encoded(capture()) == encoded(snapshot), "metadata_restore_not_exact"
    print("EXACT_METADATA_ROLLBACK_OK")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["snapshot", "migrate", "validate", "restore", "compare", "fail-after-parcel"])
    parser.add_argument("--site", required=True)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--db-host", required=True)
    args = parser.parse_args()
    # Reject failure injection before initializing or connecting to any site.
    if args.mode == "fail-after-parcel":
        assert args.db_host.startswith("shipment-check-"), "failure_injection_requires_isolated_database"
    frappe.init(args.site, sites_path="/home/frappe/frappe-bench/sites")
    assert frappe.conf.db_host == args.db_host, "unexpected_database_host"
    if args.db_host.startswith("shipment-check-"):
        assert all((urlparse(frappe.conf.get(key) or "").hostname or "").startswith("shipment-check-") for key in ("redis_cache", "redis_queue", "redis_socketio")), "unexpected_isolation_redis_host"
    frappe.connect()
    frappe.set_user("Administrator")
    try:
        if args.mode == "snapshot":
            assert not args.snapshot.exists(), "snapshot_exists"
            temporary = args.snapshot.with_suffix(".pending")
            temporary.write_bytes(encoded(capture()))
            temporary.chmod(0o600)
            os.replace(temporary, args.snapshot)
            print("METADATA_SNAPSHOT", hashlib.sha256(args.snapshot.read_bytes()).hexdigest())
            return
        snapshot = json.loads(args.snapshot.read_text())
        assert snapshot["site"] == args.site
        if args.mode == "restore":
            restore(snapshot)
        elif args.mode == "validate":
            validate(snapshot)
        elif args.mode == "compare":
            assert encoded(capture()) == encoded(snapshot), "snapshot_differs"
            print("SNAPSHOT_EXACT_MATCH")
        else:
            if args.mode == "fail-after-parcel":
                original = frappe.reload_doc
                def injected_failure(*arguments, **kwargs):
                    original(*arguments, **kwargs)
                    if arguments[:3] == ("stock", "doctype", "shipment_parcel"):
                        raise RuntimeError("ISOLATED_INJECTED_FAILURE_AFTER_PARCEL")
                frappe.reload_doc = injected_failure
                frappe.get_attr(PATCH)()
                raise AssertionError("failure_not_injected")
            before = semantic(capture())
            results = [frappe.get_attr(PATCH)(), frappe.get_attr(SF_SYNC)()]
            frappe.db.commit()
            validate(snapshot)
            after = semantic(capture())
            print(json.dumps({"result": "MIGRATION_OK", "before": before, "after": after, "actions": results}, default=json_handler))
    finally:
        frappe.destroy()


if __name__ == "__main__":
    main()
