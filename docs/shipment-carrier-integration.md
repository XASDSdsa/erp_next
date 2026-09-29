# Shipment 承运商接入边界

## 原生工作流与承运商职责

Shipment 是 ERPNext 的系统单据。ERPNext 负责原生表单、默认值、地址与联系人、人工物流、关联 Delivery Note / Sales Order 的本地生命周期、权限检查、列表和物流概况。Delivery Note 保留原生 Create → Shipment 入口；用户在 Shipment 中选择承运商，再执行该承运商的操作。

商品及贴纸展示属于原生 Shipment。`__onload.shipment_contents` 提供按 Delivery Note 分组的只读数据，`shipment_contents` HTML 字段负责显示；无权读取时使用 `shipment_contents_restricted` 提示。展示数据不写入业务子表，也不改写全局 `frappe.meta`。Delivery Note 的原生物流概况读取 `__onload.shipping_state`，其单号、状态、运输与运费来源是关联 Shipment。

SF app 只负责 SF 身份识别、外部下单、历史面单、打印、轨迹、拦截、换单、SF 运费账单及其会计处理。其他承运商应在自己的 app 实现同样的扩展边界，不把逻辑加到 SF app，也不替换 Shipment controller 或通用表单事件。取消本地来源单据不等于取消外部运单；外部操作必须是明确的承运商业务动作。

## Python registry

承运商 app 在自己的 `hooks.py` 注册模块路径：

```python
shipment_carrier_adapters = ["my_carrier.carrier_adapter"]
```

ERPNext 的 `erpnext.stock.doctype.shipment.carriers.iter_carriers()` 只加载已安装 app 的注册模块。`get_carrier(doc)` 选择匹配的 adapter；多个 adapter 同时匹配会报配置错误。使用明确的 `service_provider` 身份，旧 `carrier` 文本只应在没有明确 provider 时作为兼容依据。

当前 adapter 接口如下：

| 接口 | 要求 | 返回值与约束 |
| --- | --- | --- |
| `matches(doc)` | 必需 | 布尔值；仅匹配自己的承运商，不能接管人工物流或其他 provider。 |
| `has_booking(doc)` | 必需 | 布尔值；只查询本地当前及历史下单证据。即使选择器被改动，也不能遗漏历史外部运单；不得调用外部下单或取消 API。 |
| `get_display_values(doc)` | 必需 | 字典；可返回 `transport_status_display`、`freight_status_display`、`interception_status_display`、`label_replacement_display` 的部分或全部展示值。ERPNext 管理这些派生字段的持久化。没有特有状态时可返回 `{}`。 |
| `get_delivery_note_summary(doc)` | 可选 | 字典；在原生 Shipment 读取权限检查后添加承运商自己的摘要字段，不能覆盖原生单据身份与状态字段。 |
| `get_sales_order_freight_summary(names)` | 可选 | `{sales_order_name: {text, color, extra, title}}`；只汇总本承运商且当前用户可读取的运费事实，不返回无关承运商或人工物流。 |

Sales Order 列表使用原生 `shipment_list_api.get_sales_orders`，保留 `reportview.get()` 的查询、权限、行顺序及响应字段，在行上追加通用 `shipment_freight` 数据。ERPNext 自行生成原生人工物流摘要，再与各 adapter 的展示字典组合。`color` 使用 `gray`、`green`、`blue`、`orange`、`red`；展示字段是纯文本，前端转义后渲染。

运费金额必须明确币种；不能把不同币种相加，也不能用未知值冒充零。人工登记、承运商账单确认、会计记账是不同事实。原生层不解释承运商私有状态，也不把展示字典中的金额合计。运费源抛出权限错误时，列表保留订单但显示权限提示，不展示不完整的部分汇总；其他错误不会伪装成无运费。

## 表单 UI 接入

承运商 app 可通过 `doctype_js = {"Shipment": "public/js/shipment.js"}` 加载自己的扩展。原生脚本先建立 registry：

```javascript
erpnext.shipment.register_carrier("my_carrier", {
	matches: (doc) => doc.service_provider === "My Carrier",
	owns_api_ui: true,
});
```

| 配置 | 要求 | 作用 |
| --- | --- | --- |
| `matches(doc)` | 必需 | 与服务端身份规则一致；各 provider 的条件必须互斥。 |
| `owns_api_ui` | 可选，默认否 | 此 provider 提供自己的 API 按钮；原生 `frm.events.shipping_api_enabled(frm)` 返回否，让通用 Shipping 插件在注册按钮源头退出。 |
| `hidden_fields` | 可选 | 当前 provider 不使用的原生字段名数组；原生表单负责应用并在切换 provider 时恢复元数据。 |
| `lock_service_provider(frm)` | 可选 | 返回是否锁定 provider 选择；服务端仍需独立维护已有运单和历史的不可改归属约束。 |
| `single_column_information` | 可选，默认否 | 当前 provider 的 Shipment Information 区域使用单列布局。 |

扩展事件先检查自己的 `matches`，仅操作自己的字段、状态和按钮；异步请求需验证返回时仍是原文档与 provider。不得覆盖原生默认值或联系人事件、删除其他 app 的按钮、延迟重复隐藏按钮、全局注入翻译或修改 metadata API。第三方 Shipping 插件在能力接口存在时读取 `shipping_api_enabled(frm)`；旧 ERPNext 没有该接口时保留插件默认行为。

## 无 SF 安装与旧字段兼容

没有 SF app 或任何 adapter 时，原生 Shipment、人工物流、Delivery Note 物流概况、商品贴纸和 Sales Order 人工运费汇总均正常工作。原生模块不导入 SF 或 `erpnext_shipping`；SF app 依赖 ERPNext，但不以第三方 Shipping 插件作为通用数据模型依赖。

`erpnext.stock.doctype.shipment.delivery_note_update.update_delivery_note(delivery_notes, shipment_info=None, tracking_info=None)` 是历史 Delivery Note 投影的内部兼容接口。它仅更新 Delivery Note metadata 中存在的 `delivery_type`、`parcel_service`、`parcel_service_type`、`tracking_number`、`tracking_url`、`tracking_status`、`tracking_status_info`，每张单据一次 `db_set`。这些旧 Custom Field 不存在时直接返回，不创建字段、不要求安装 Shipping 插件。

该 helper 沿用已授权调用方与后台任务的直接投影语义，不新增独立远程入口、不代替上游 API 的权限检查，也不强加新的交互用户权限。它不提交事务；投影失败会传给调用方处理。原生界面仍以 Shipment 为权威数据源。

## 元数据迁移与历史保留

`erpnext.patches.v16_0.move_shipment_carrier_metadata` 把已确认的旧通用字段和布局归回原生 DocType。它检查旧 Custom Field 定义、定制覆盖及布局冲突；遇到客户定制会停止，先保留并处理定制再重试。只移除确认属于旧安装器的重复 metadata，不删除 Shipment、Delivery Note、人工运单、SF Waybill、账单或会计业务记录，也不清空历史单号、数据库列和唯一性索引。

迁移后各承运商 app 只同步自己拥有的字段与配置。历史外部下单证据继续参与 provider 变更约束；接入新承运商应创建独立 Shipment，不能通过改 selector 抹去已有运单身份。部署时使用已审查的窄迁移步骤，并单独验证无承运商 app、人工物流和各已安装 provider 的正常场景。
