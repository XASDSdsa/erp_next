"""USD PayPal receipt accounting; transaction ownership stays with paypal_receipt."""

from collections import defaultdict
from decimal import Decimal, InvalidOperation

import frappe
from frappe.utils import flt


COMPANY = "莱亚国际"
BANK = "PayPal 孙灵赞 USD - LEYA"
EXPENSE = "PayPal 手续费 USD - LEYA"
RECEIVABLE = "美元应收账款 - LEYA"
COST_CENTER = "主 - LEYA"
MODE = "PayPal Payment"
CURRENCY = "USD"
CENT = Decimal("0.01")


def require(condition, message):
	if not condition:
		frappe.throw(message)


def decimal_value(value, label="金额"):
	try:
		number = Decimal(str(value))
	except (InvalidOperation, TypeError, ValueError):
		frappe.throw(f"{label}必须为有效数字")
	require(number.is_finite() and abs(number) < Decimal("1000000000"), f"{label}必须为有限有效数字")
	return number


def money(value, label="金额"):
	number = decimal_value(value, label)
	require(number == number.quantize(CENT), f"{label}最多两位小数")
	return number.quantize(CENT)


def validate_amounts(gross, fee, net):
	values = {"gross": money(gross, "付款金额"), "fee": money(fee, "手续费"), "net": money(net, "净到账")}
	require(values["gross"] > 0 and values["fee"] >= 0 and values["net"] > 0, "付款及净到账必须为正数，手续费不能为负数")
	require(values["gross"] - values["fee"] == values["net"], "USD付款、手续费和净到账不平衡")
	return values


def _load(doctype, name):
	doc = frappe.get_doc(doctype, name)
	doc.check_permission("read")
	return doc


def _order_identity(order):
	order.check_permission("read")
	require(order.company == COMPANY, "订单公司不匹配")
	require(order.currency == CURRENCY, "仅支持USD销售订单收款")
	require(not order.get("party_account_currency") or order.party_account_currency == CURRENCY, "订单往来币种必须为USD")


def assert_new_order_allowed(order, gross):
	"""Reject invoiced orders instead of treating advance_paid as invoice payments."""
	_order_identity(order)
	require(order.docstatus == 1 and order.status not in ("Closed", "Cancelled", "On Hold"), "订单状态不允许收款")
	party = _load("Customer", order.customer)
	require(not party.get("disabled") and not party.get("is_frozen"), "客户已停用或冻结")
	# This is a safety exclusion, not a list of invoices disclosed to the caller.
	invoiced = frappe.db.sql(
		"""select si.name from `tabSales Invoice Item` sii
		inner join `tabSales Invoice` si on si.name = sii.parent
		where sii.sales_order = %s and si.docstatus = 1 limit 1""",
		(order.name,),
	)
	require(not decimal_value(order.get("per_billed") or 0) and not invoiced, "订单已开票，请通过对应销售发票登记收款，不能继续按订单预收款记账")
	total = order.grand_total if order.get("disable_rounded_total") or not order.get("rounded_total") else order.rounded_total
	remaining = money(total, "订单金额") - money(order.get("advance_paid") or 0, "订单预收款")
	require(money(gross) <= remaining, "超过订单可收余额")
	return remaining


validate_order_for_new_receipt = assert_new_order_allowed


def _valid_account(name, currency, account_type=None):
	account = _load("Account", name)
	require(account.company == COMPANY and account.account_currency == currency, f"科目{name}公司或币种错误")
	require(not account.get("disabled") and not account.get("is_group"), f"科目{name}已停用或为分组")
	if account_type:
		require(account.account_type == account_type, f"科目{name}类型错误")
	return account


def _valid_center(name):
	require(name, "未配置成本中心")
	center = _load("Cost Center", name)
	require(center.company == COMPANY and not center.get("disabled") and not center.get("is_group"), "成本中心不可用")


def _configuration():
	require(frappe.has_permission("GL Entry", ptype="read"), "需要总账读取权限才能核验收款；尚未创建凭证")
	company = _load("Company", COMPANY)
	require(company.get("default_currency"), "公司缺少本位币")
	for name, kind in ((BANK, "Bank"), (EXPENSE, "Expense Account"), (RECEIVABLE, "Receivable")):
		_valid_account(name, CURRENCY, kind)
	_valid_center(COST_CENTER)
	require(_load("Mode of Payment", MODE).get("enabled"), "PayPal支付方式未启用")
	return company


def _precision(row, field):
	value = row.precision(field)
	return int(value) if value is not None else 2


def _rounded(value, precision):
	# Match the site's native rounding mode and float multiplication in Journal Entry.
	return Decimal(str(flt(float(value), precision)))


def _base_amount(amount, rate, precision):
	return Decimal(str(flt(float(amount) * float(rate), precision)))


def _roundoff_details(company):
	account_name = company.get("round_off_account")
	center = company.get("round_off_cost_center")
	require(account_name and center, "人民币折算存在一分钱尾差，请先配置公司的舍入科目及成本中心")
	account = _valid_account(account_name, company.default_currency)
	require(account.get("root_type") == "Expense" and account.get("account_type") not in ("Receivable", "Payable"), "舍入科目必须是公司本位币费用科目")
	_valid_center(center)
	return account_name, center


def build_receipt(order, amounts, posting_date, transaction_id, reference_date):
	"""Build an unsaved native Journal Entry with a trusted, dated exchange rate."""
	_order_identity(order)
	amounts = validate_amounts(**amounts)
	company = _configuration()
	from erpnext.setup.utils import get_exchange_rate

	rate = decimal_value(get_exchange_rate(CURRENCY, company.default_currency, posting_date), "汇率")
	require(rate > 0, "未取得有效USD本位币汇率，尚未创建凭证；请先维护对应日期汇率")
	entries = []
	for account, debit, credit in (
		(BANK, amounts["net"], Decimal(0)),
		(EXPENSE, amounts["fee"], Decimal(0)),
		(RECEIVABLE, Decimal(0), amounts["gross"]),
	):
		if not debit and not credit:
			continue
		row = dict(account=account, account_currency=CURRENCY, debit_in_account_currency=float(debit),
			credit_in_account_currency=float(credit), exchange_rate=float(rate), cost_center=COST_CENTER)
		if account == RECEIVABLE:
			row.update(party_type="Customer", party=order.customer, reference_type="Sales Order", reference_name=order.name, is_advance="Yes")
		entries.append(row)
	doc = frappe.get_doc(dict(doctype="Journal Entry", voucher_type="Bank Entry", company=COMPANY,
		posting_date=posting_date, company_currency=company.default_currency, multi_currency=1,
		cheque_no=transaction_id, cheque_date=reference_date, mode_of_payment=MODE,
		user_remark=f"PayPal USD receipt for {order.name}; gross={amounts['gross']}, fee={amounts['fee']}, net={amounts['net']}", accounts=entries))
	doc.check_permission("create")
	doc.check_permission("submit")
	for row in doc.accounts:
		row.exchange_rate = float(_rounded(rate, _precision(row, "exchange_rate")))
		require(row.exchange_rate > 0, "汇率精度不足，尚未创建凭证")
		for side in ("debit", "credit"):
			row.set(side, float(_base_amount(row.get(f"{side}_in_account_currency") or 0, row.exchange_rate, _precision(row, side))))
	difference = sum((decimal_value(row.debit or 0) - decimal_value(row.credit or 0) for row in doc.accounts), Decimal(0))
	if difference:
		require(abs(difference) <= CENT, "本位币折算差额超过一分钱，停止收款")
		account, center = _roundoff_details(company)
		doc.append("accounts", dict(account=account, account_currency=company.default_currency,
			cost_center=center, exchange_rate=1, debit_in_account_currency=float(max(-difference, Decimal(0))),
			credit_in_account_currency=float(max(difference, Decimal(0))),
			debit=float(max(-difference, Decimal(0))), credit=float(max(difference, Decimal(0))),
			user_remark="PayPal本位币折算尾差（不改变美元收款金额）"))
	# A verified rate of one must not trigger Journal Entry's fallback-to-one lookup.
	doc.flags.ignore_exchange_rate = True
	doc.flags.paypal_expected_exchange_rate = str(doc.accounts[0].exchange_rate)
	return doc


def _source_matches(row, order, *, ledger=False, voucher=None):
	ref_type = row.get("against_voucher_type" if ledger else "reference_type")
	ref_name = row.get("against_voucher" if ledger else "reference_name")
	source_matches = row.get("advance_voucher_type") == "Sales Order" and row.get("advance_voucher_no") == order.name
	if ref_type == "Sales Order" and ref_name == order.name:
		return True
	if ledger and ref_type == "Journal Entry" and ref_name == voucher:
		# Older native ledgers may lack source metadata; the JE rows establish it.
		return not row.get("advance_voucher_no") or source_matches
	if ref_type == "Sales Invoice" and source_matches and ref_name:
		invoice = _load("Sales Invoice", ref_name)
		return invoice.company == COMPANY and invoice.customer == order.customer and invoice.currency == CURRENCY and invoice.docstatus == 1
	return False


def _amount_totals(rows):
	result = defaultdict(lambda: {key: Decimal(0) for key in ("debit_in_account_currency", "credit_in_account_currency", "debit", "credit")})
	for row in rows:
		for key in result[row.get("account")]:
			result[row.get("account")][key] += decimal_value(row.get(key) or 0)
	return dict(result)


def verify_receipt(doc, order, amounts, posting_date, transaction_id, reference_date, *, check_gl=True, new_receipt=False):
	"""Verify source-aware account aggregates, including native invoice allocations."""
	amounts = validate_amounts(**amounts)
	_order_identity(order)
	doc.check_permission("read")
	require(doc.company == COMPANY and doc.voucher_type == "Bank Entry" and doc.mode_of_payment == MODE, "凭证公司、类型或支付方式错误")
	require(str(doc.cheque_no or "").strip().upper() == transaction_id and str(doc.cheque_date) == reference_date and str(doc.posting_date) == posting_date, "凭证交易号或日期错误")
	require(doc.docstatus in (0, 1) and (not check_gl or doc.docstatus == 1), "凭证未提交或已取消")
	company = _load("Company", COMPANY)
	rows = doc.get("accounts") or []
	require(rows, "凭证缺少明细")
	precision = _precision(rows[0], "debit")
	expected = {BANK: (amounts["net"], Decimal(0)), RECEIVABLE: (Decimal(0), amounts["gross"])}
	if amounts["fee"]:
		expected[EXPENSE] = (amounts["fee"], Decimal(0))
	roundoff_name = company.get("round_off_account")
	usd_rates = []
	allocated = False
	for row in rows:
		account = row.get("account")
		require(account in expected or (roundoff_name and account == roundoff_name), "凭证出现未预期科目")
		currency = CURRENCY if account in expected else company.default_currency
		require(row.get("account_currency") == currency, "凭证明细币种错误")
		rate = decimal_value(row.get("exchange_rate"), "凭证汇率")
		require(rate > 0 and (currency != company.default_currency or rate == 1), "凭证汇率无效")
		for side in ("debit", "credit"):
			amount = money(row.get(f"{side}_in_account_currency") or 0)
			require(amount >= 0, "凭证明细金额不能为负数")
			actual_base = _rounded(decimal_value(row.get(side) or 0), _precision(row, side))
			require(actual_base == _base_amount(amount, rate, _precision(row, side)), "凭证本位币折算金额与汇率不符")
		if account in expected:
			require(row.get("cost_center") == COST_CENTER, "凭证成本中心错误")
			usd_rates.append(rate)
			if account == RECEIVABLE:
				require(row.get("party_type") == "Customer" and row.get("party") == order.customer and row.get("is_advance") == "Yes", "凭证客户或预收标记错误")
				require(_source_matches(row, order), "凭证来源销售订单或发票分配关联错误")
				allocated = allocated or row.get("reference_type") == "Sales Invoice"
				if new_receipt:
					require(row.get("reference_type") == "Sales Order" and row.get("reference_name") == order.name, "新收款必须关联目标销售订单")
			else:
				require(not row.get("party") and not row.get("reference_name"), "PayPal或手续费行存在异常往来引用")
		else:
			require(row.get("cost_center") == company.get("round_off_cost_center") and not row.get("party") and not row.get("reference_name"), "舍入行成本中心或引用错误")
			require(abs(decimal_value(row.get("debit") or 0)) + abs(decimal_value(row.get("credit") or 0)) <= CENT, "舍入金额超过一分钱")
	if new_receipt:
		expected_rate = doc.flags.get("paypal_expected_exchange_rate")
		require(expected_rate and all(rate == decimal_value(expected_rate) for rate in usd_rates), "新收款的本位币汇率与已核实汇率不一致")
	totals = _amount_totals(rows)
	for account, (debit, credit) in expected.items():
		require(account in totals and totals[account]["debit_in_account_currency"] == debit and totals[account]["credit_in_account_currency"] == credit, "凭证USD净到账、手续费或应收金额不符")
	if roundoff_name in totals:
		roundoff = totals[roundoff_name]
		require(roundoff["debit"] + roundoff["credit"] <= CENT, "舍入行合计超过一分钱")
	difference = _rounded(sum((value["debit"] - value["credit"] for value in totals.values()), Decimal(0)), precision)
	# Native reconciliation can split a receivable row and post the resulting
	# rounding cent directly in GL without adding a Journal Entry Account row.
	native_roundoff = not new_receipt and allocated and difference and abs(difference) <= CENT
	require(not difference or native_roundoff, "凭证本位币借贷不平衡")
	ledger_expected = {account: dict(values) for account, values in totals.items()}
	if native_roundoff:
		_roundoff_details(company)
		without_roundoff = sum((value["debit"] - value["credit"] for account, value in totals.items() if account != roundoff_name), Decimal(0))
		without_roundoff = _rounded(without_roundoff, precision)
		require(abs(without_roundoff) <= CENT, "原生分配后的舍入金额超过一分钱")
		if without_roundoff:
			debit, credit = max(-without_roundoff, Decimal(0)), max(without_roundoff, Decimal(0))
			ledger_expected[roundoff_name] = dict(debit=debit, credit=credit, debit_in_account_currency=debit, credit_in_account_currency=credit)
		else:
			ledger_expected.pop(roundoff_name, None)
	gl_references = []
	if check_gl:
		require(frappe.has_permission("GL Entry", ptype="read"), "需要总账读取权限才能核验收款")
		fields = ["account", "account_currency", "debit_in_account_currency", "credit_in_account_currency", "debit", "credit", "party_type", "party", "against_voucher_type", "against_voucher", "company", "posting_date", "cost_center"]
		# Source-order fields live on Journal Entry Account, not GL Entry. Those
		# rows were verified above; ledger references must match their allocations.
		ledger_references = set()
		for row in rows:
			if row.get("account") != RECEIVABLE:
				continue
			ledger_references.add((row.get("reference_type"), row.get("reference_name")))
			if row.get("reference_type") == "Sales Order":
				ledger_references.add(("Journal Entry", doc.name))
		# Authorization is checked first; read the complete ledger, never a permission-filtered subset.
		ledger = frappe.get_all("GL Entry", filters={"voucher_type": "Journal Entry", "voucher_no": doc.name, "is_cancelled": 0}, fields=fields, limit_page_length=10001)
		require(ledger and len(ledger) <= 10000, "总账为空或超过核验范围")
		for row in ledger:
			account = row.get("account")
			require(account in ledger_expected and row.get("company") == COMPANY and str(row.get("posting_date")) == posting_date, "总账科目、公司或日期错误")
			currency = CURRENCY if account in expected else company.default_currency
			center = COST_CENTER if account in expected else company.get("round_off_cost_center")
			require(row.get("account_currency") == currency and row.get("cost_center") == center, "总账币种或成本中心错误")
			for key in ("debit", "credit", "debit_in_account_currency", "credit_in_account_currency"):
				require(decimal_value(row.get(key) or 0) >= 0, "总账包含异常负数")
			if account == RECEIVABLE:
				require(row.get("party_type") == "Customer" and row.get("party") == order.customer and (row.get("against_voucher_type"), row.get("against_voucher")) in ledger_references, "总账客户或销售订单来源错误")
				gl_references.append({"doctype": row.get("against_voucher_type"), "name": row.get("against_voucher")})
			else:
				require(not row.get("party"), "总账非应收行存在异常客户")
		actual_totals = _amount_totals(ledger)
		require(actual_totals.keys() == ledger_expected.keys(), "总账科目缺失或多余")
		for account, values in ledger_expected.items():
			for key, value in values.items():
				actual = actual_totals[account][key]
				require((actual == value) if key.endswith("_in_account_currency") else (_rounded(actual, precision) == _rounded(value, precision)), "总账USD或本位币金额与凭证不符")
		require(_rounded(sum((value["debit"] - value["credit"] for value in actual_totals.values()), Decimal(0)), precision) == 0, "总账本位币借贷不平衡")
	return {"verified": True, "submitted": doc.docstatus == 1, "gl_generated": bool(check_gl), "journal_entry": doc.name,
		"sales_order": order.name, "customer": order.customer, "currency": CURRENCY, **{key: float(value) for key, value in amounts.items()},
		"gl_references": gl_references, "company_currency": company.default_currency,
		"actual_receipt_currency": CURRENCY,
		"actual_receipt_note": f"PayPal实际收款、手续费和净到账均为 {CURRENCY}。",
		"company_base_currency": company.default_currency,
		"base_currency_note": f"{company.default_currency}仅用于ERP总账和报表折算，不代表实际收到{company.default_currency}，也不代表发生换汇或提现。",
		"base_debit": float(sum((value["debit"] for value in ledger_expected.values()), Decimal(0))),
		"base_credit": float(sum((value["credit"] for value in ledger_expected.values()), Decimal(0)))}
