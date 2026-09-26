# 公共采购密封投标与评审系统

标准库实现的招标发布、密封投标、开标校验收、规则评分、利益冲突、澄清、废标、投诉重评和授标快照服务。

## 运行

要求 Python 3.11+（当前 Python 3.9 环境亦可）。

```bash
python3 app.py --init --seed
python3 app.py
```

默认地址 `http://127.0.0.1:8209`，数据库默认 `public_procurement.db`。

## 主要接口

使用 `X-User`、`X-Role` 请求头。角色有 `procurement`、`vendor`、`evaluator`、`supervisor`、`auditor`、`public`。

- `GET /health`、`GET /api/state`、`GET /api/tenders/{id}`
- `POST /api/vendors`、`POST /api/tenders`、`POST /api/tenders/publish`
- `POST /api/bids`、`POST /api/bids/withdraw`、`POST /api/bids/disqualify`
- `POST /api/tenders/open`：截止后开标并核验承诺哈希
- `POST /api/conflicts`、`POST /api/evaluations`
- `POST /api/clarifications`、`POST /api/clarifications/answer`
- `POST /api/complaints`、`POST /api/complaints/resolve`
- `POST /api/tenders/award`：锁定评分轮次并保存排名快照

## 履约验收台

页面 `/performance`。合同、节点、完成量报送、验收、应付、付款与版本快照分表存储（金额一律整数分落库）。

- `POST /api/contracts`：监督员从**已授标**项目建合同并拆分节点金额，节点合计必须等于中标价；同一授标项目只能建一份合同
- `POST /api/performance/reports`：中标供应商按节点报完成量百分比
- `POST /api/performance/acceptances`：采购人对报量验收。同一节点只保留一条有效验收（`review`/`accepted` 部分唯一索引），重复提交返回 `409`；累计验收超过节点金额或合同额直接 `409` 拦截；验收金额与报量比例匹配额不一致时停在 `review`，不形成应付
- `POST /api/performance/acceptances/resolve`：监督员复核（通过可修正金额 / 驳回），通过后生成应付
- `POST /api/performance/payments`：监督员针对应付登记付款。节点超付或累计超过合同额时停在 `review` 不生效，金额内付款直接入账
- `POST /api/performance/payments/resolve`：监督员复核付款，超额度的确认仍被拒绝
- `GET /api/contracts/overview`：按角色返回合同列表和待办（供应商待报量 / 采购人待验收 / 监督员待复核）
- `GET /api/contracts/{id}`：按项目展示节点当前状态、验收、应付和付款；`GET /api/versions/{contract|node|acceptance|payment}/{id}` 查版本链
- 公开角色（`public`）只看履约状态（合同/节点/验收状态），不返回合同额、节点金额、已验收/已付等任何金额明细，`GET /api/state` 的 `performance` 字段同样脱敏

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整开标授标、截止前正文隐藏、利益冲突、重复评分覆盖、投诉重评和角色权限；履约测试覆盖建合同拆节点、报量-验收-付款全链路、金额不匹配进待复核、同节点重复验收 409、累计验收/付款封顶、节点超付挂起、合同结清、版本链、角色权限和公开页金额脱敏。

## 局限

供应商与请求用户没有绑定校验，身份仍依赖请求头；投标正文虽然按接口阶段隐藏，但数据库本身未加密；评分规则适合演示，不覆盖复杂资格预审、保证金、电子签名和采购法规差异。
