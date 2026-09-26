# 公共采购密封投标与评审系统

标准库实现的招标发布、密封投标、开标校验收、规则评分、利益冲突、澄清、废标、投诉重评、授标快照和履约验收服务。

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
- `POST /api/contracts`：监督员从已授标项目建合同并拆分节点金额（节点合计须等于合同额，合同额默认取中标价）
- `POST /api/performance/report`：供应商填报节点完成量（0-100 的完成比例）
- `POST /api/performance/accept`：采购人验收形成应付记录
- `POST /api/performance/pay`：监督员登记付款
- `POST /api/performance/review`：监督员复核待复核的验收或付款（`confirm` 确认有效 / `void` 作废）
- `GET /api/tenders/{id}/performance`：按项目查看合同、当前节点、待办、验收付款与版本快照

## 履约验收台

- 流程：监督员建合同拆分节点 → 供应商报完成量 → 采购人验收形成应付记录 → 监督员登记付款。
- 验收应付期望金额为 `节点金额 × 完成量 / 100`；金额不匹配、超节点金额或累计验收超合同额的验收记录停在 `pending_review`（待复核），复核确认时再次校验合同额上限。
- 节点超付或累计付款超合同额的付款同样停在待复核；有效验收与有效付款的累计值永远不会超过节点金额与合同额。
- 同一节点只保留一条有效（含待复核）验收，重复提交返回 409；验收作废后节点回到已报量状态，可重新验收。
- 合同、节点、验收、付款分表存储，每次变更在 `contract_versions` 落一份完整快照。
- `/api/state` 与项目履约视图按角色返回：采购、监督、审计看金额明细与待办，供应商只看自己中标的合同，公开视图只看履约状态（节点名称与状态），不暴露金额明细。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整开标授标、截止前正文隐藏、利益冲突、重复评分覆盖、投诉重评、角色权限，以及履约验收（建合同拆分节点、报量验收付款、超付与金额不匹配待复核、重复验收冲突、版本快照、公开视图隐藏金额）。

## 局限

供应商与请求用户没有绑定校验，身份仍依赖请求头；投标正文虽然按接口阶段隐藏，但数据库本身未加密；评分规则适合演示，不覆盖复杂资格预审、保证金、电子签名和采购法规差异。
