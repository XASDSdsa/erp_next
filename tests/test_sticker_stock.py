"""Only customer stickers require valued physical stock before delivery."""
import ast
import importlib.util
from pathlib import Path
import sys
import types
from unittest.mock import Mock
import pytest

class Row(dict):
    __getattr__ = dict.get
    __setattr__ = dict.__setitem__

@pytest.fixture
def env(monkeypatch):
    f=types.ModuleType('frappe')
    f.throw=lambda message, **kwargs: (_ for _ in ()).throw(ValueError(message))
    f.get_all=Mock(return_value=[Row(customer_name='Customer-A')])
    items={'S':Row(name='S',item_name='客户贴纸',variant_of='巧克粉贴纸',is_stock_item=1,stock_uom='张'),
           'P':Row(name='P',item_name='巧克粉',is_stock_item=1,stock_uom='颗')}
    f.get_doc=lambda doctype,name: items[name]
    f.db=Row(sql=Mock())
    util=types.ModuleType('frappe.utils')
    util.cint=lambda v:int(v or 0)
    util.flt=lambda v:float(v or 0)
    util.nowdate=lambda:'2026-09-18'
    util.nowtime=lambda:'12:00:00'
    monkeypatch.setitem(sys.modules,'frappe',f)
    monkeypatch.setitem(sys.modules,'frappe.utils',util)
    spec=importlib.util.spec_from_file_location('sticker_stock_test',Path(__file__).parents[1]/'erpnext/stock/doctype/item/sticker_stock.py')
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    balance=m._balance
    m._balance=Mock(return_value=(100,0.6))
    row=Row(item_code='S',qty=100,conversion_factor=1,warehouse='W')
    doc=Row(docstatus=0,items=[row],packed_items=[])
    return Row(f=f,m=m,items=items,doc=doc,row=row,native_balance=balance,monkeypatch=monkeypatch)


def test_out_of_stock_block_includes_exact_shortage(env):
    env.m._balance.return_value=(40,0.6)
    with pytest.raises(ValueError,match='需要 100 张.*可用 40.*缺少 60'):
        env.m.validate_delivery_sticker_stock(env.doc)


def test_duplicate_and_packed_stickers_aggregate_in_stock_units(env):
    env.row.update(qty=3,conversion_factor=20)
    env.doc['items'].append(Row(item_code='S',qty=2,conversion_factor=20,warehouse='W'))
    env.doc.packed_items=[Row(item_code='S',qty=10,warehouse='W')]
    with pytest.raises(ValueError,match='需要 110 张.*缺少 10'):
        env.m.validate_delivery_sticker_stock(env.doc)
    assert env.m._balance.call_count==1


def test_different_warehouses_do_not_share_stock(env):
    env.doc['items'].append(Row(item_code='S',qty=10,conversion_factor=1,warehouse='EMPTY'))
    env.m._balance.side_effect=lambda code,wh,*args:(0 if wh=='EMPTY' else 100,0.6)
    with pytest.raises(ValueError,match='EMPTY.*缺少 10'):
        env.m.validate_delivery_sticker_stock(env.doc)


def test_chalk_stock_policy_is_untouched(env):
    env.doc.items=[Row(item_code='P',qty=10000,conversion_factor=1,warehouse='W')]
    assert env.m.validate_delivery_sticker_stock(env.doc)==[]
    env.m._balance.assert_not_called()


def test_free_sticker_still_needs_stock_and_real_cost(env):
    env.row.update(rate=0,allow_zero_valuation_rate=1)
    env.m._balance.return_value=(100,0)
    with pytest.raises(ValueError,match='入库成本'):
        env.m.validate_delivery_sticker_stock(env.doc)


def test_non_stock_bundle_parent_still_requires_actual_sticker_stock(env):
    env['items']['BUNDLE'] = Row(name='BUNDLE', item_name='巧克粉加客户贴纸', is_stock_item=0)
    env.doc['items'] = [Row(name='DNI', item_code='BUNDLE', qty=120, conversion_factor=1, warehouse='W')]
    env.doc.packed_items = [Row(parent_detail_docname='DNI', parent_item='BUNDLE', item_code='P', qty=120, warehouse='W'),
                            Row(parent_detail_docname='DNI', parent_item='BUNDLE', item_code='S', qty=120, warehouse='W')]
    with pytest.raises(ValueError, match='需要 120 张.*缺少 20'):
        env.m.validate_delivery_sticker_stock(env.doc)
    env.m._balance.assert_called_once_with('S', 'W', '2026-09-18', '12:00:00')


def test_non_stock_sticker_is_rejected_before_balance(env):
    env['items']['S'].is_stock_item=0
    with pytest.raises(ValueError,match='维护库存'):
        env.m.validate_delivery_sticker_stock(env.doc)
    env.m._balance.assert_not_called()


@pytest.mark.parametrize('status,is_return,action,checks',[(0,0,None,1),(1,0,'submit',1),(1,0,'update_after_submit',0),(2,0,'cancel',0),(0,1,None,0)])
def test_save_and_submit_are_checked_but_returns_and_existing_submits_are_not(env,status,is_return,action,checks):
    env.doc.update(docstatus=status,is_return=is_return,_action=action)
    env.m.validate_delivery_sticker_stock(env.doc)
    assert env.m._balance.call_count==checks


def test_submit_rechecks_stock_spent_after_draft(env):
    env.m.validate_delivery_sticker_stock(env.doc)
    env.m._balance.return_value=(20,0.6)
    env.doc.update(docstatus=1,_action='submit')
    with pytest.raises(ValueError,match='缺少 80'):
        env.m.validate_delivery_sticker_stock(env.doc)


def test_legacy_identity_requires_unique_exact_customer(env):
    item=Row(name='巧克粉贴纸-Customer-A-方形-v1',attributes=[])
    assert env.m.is_customer_sticker(item)
    env.f.get_all.return_value=[Row(customer_name='Customer-A'),Row(customer_name='Customer-A')]
    assert not env.m.is_customer_sticker(item)
    assert not env.m.is_customer_sticker(Row(name='普通贴纸包装说明'))


def test_balance_locks_bin_and_rejects_backdated_future_shortage(env):
    ledger=types.ModuleType('erpnext.stock.stock_ledger')
    ledger.get_previous_sle=Mock(return_value=Row(qty_after_transaction=100,valuation_rate=0.6))
    util=types.ModuleType('erpnext.stock.utils');util.get_combine_datetime=lambda d,t:d+' '+t
    env.monkeypatch.setitem(sys.modules,'erpnext.stock.stock_ledger',ledger)
    env.monkeypatch.setitem(sys.modules,'erpnext.stock.utils',util)
    env.f.db.sql.side_effect=[[Row(actual_qty=200,valuation_rate=0.6)],
        [Row(qty_after_transaction=5,voucher_type='Delivery Note'),Row(qty_after_transaction=200,voucher_type='Purchase Receipt')]]
    assert env.native_balance('S','W','2026-09-17','12:00:00')==(5,0.6)
    assert all('for update' in call.args[0] for call in env.f.db.sql.call_args_list)
    assert ledger.get_previous_sle.call_args.kwargs['for_update']


@pytest.fixture
def customer_env(env):
    env.f.get_all.return_value = ['CUST-001']
    env.f.db.exists = Mock(return_value=True)
    env.customer = Row(name='CUST-001', check_permission=Mock())
    env.f.get_doc = Mock(return_value=env.customer)
    env.sticker = Row(name='STICKER-1', variant_of=env.m.TEMPLATE,
                      attributes=[Row(attribute='客户', attribute_value='Customer-A')],
                      is_stock_item=1, is_purchase_item=1)
    return env


@pytest.mark.parametrize('legacy', [False, True])
def test_sticker_customer_resolves_exact_variant_and_legacy_owner_with_permission(customer_env, legacy):
    env = customer_env
    item = Row(name='巧克粉贴纸-Customer-A-方形-v1') if legacy else env.sticker

    assert env.m.sticker_customer(item) == 'CUST-001'
    env.f.get_all.assert_called_once_with('Customer', filters={'customer_name': 'Customer-A'},
                                         pluck='name', limit_page_length=2)
    env.f.db.exists.assert_called_once_with('Customer', 'CUST-001')
    env.f.get_doc.assert_called_once_with('Customer', 'CUST-001')
    env.customer.check_permission.assert_called_once_with('read')


@pytest.mark.parametrize('matches', [[], ['CUST-001', 'CUST-002']])
def test_sticker_customer_rejects_missing_or_duplicate_customers(customer_env, matches):
    env = customer_env
    env.f.get_all.return_value = matches
    with pytest.raises(ValueError, match='无法唯一确认'):
        env.m.sticker_customer(env.sticker)
    env.f.get_doc.assert_not_called()


@pytest.mark.parametrize('attributes', [[], [Row(attribute='客户', attribute_value='Customer-A')]*2])
def test_sticker_customer_rejects_missing_or_ambiguous_customer_attributes(customer_env, attributes):
    env = customer_env
    env.sticker.attributes = attributes
    with pytest.raises(ValueError, match='无法唯一确认'):
        env.m.sticker_customer(env.sticker)
    env.f.get_all.assert_not_called()
    env.f.get_doc.assert_not_called()


def test_sticker_customer_rejects_deleted_customer(customer_env):
    env = customer_env
    env.f.db.exists.return_value = False
    with pytest.raises(ValueError, match='不存在或已删除'):
        env.m.sticker_customer(env.sticker)
    env.f.get_doc.assert_not_called()


def test_sticker_customer_does_not_bypass_customer_read_permission(customer_env):
    env = customer_env
    env.customer.check_permission.side_effect = PermissionError('Customer read denied')
    with pytest.raises(PermissionError, match='Customer read denied'):
        env.m.sticker_customer(env.sticker)
