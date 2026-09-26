"""Sealed public-procurement tendering and evaluation service."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "public_procurement.db"


class DomainError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_time(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise DomainError("时间格式无效") from exc
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def clean_actor(actor: str) -> str:
    actor = (actor or "").strip()
    if not actor:
        raise DomainError("缺少操作人")
    return actor


def require_role(role: str, allowed: set[str], action: str) -> None:
    if role not in allowed:
        raise DomainError("角色无权执行：%s" % action, 403)


def canonical_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class ProcurementService:
    def __init__(self, db_path: str | os.PathLike[str] = DEFAULT_DB):
        self.db_path = str(db_path)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tenders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tender_no TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'draft',
                    deadline TEXT NOT NULL,
                    criteria TEXT NOT NULL DEFAULT '[]',
                    evaluation_round INTEGER NOT NULL DEFAULT 1,
                    evaluations_locked INTEGER NOT NULL DEFAULT 0,
                    awarded_bid_id INTEGER,
                    award_snapshot TEXT,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS vendors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vendor_no TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    representative TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bids (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tender_id INTEGER NOT NULL REFERENCES tenders(id),
                    vendor_id INTEGER NOT NULL REFERENCES vendors(id),
                    payload TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    price REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'sealed',
                    version INTEGER NOT NULL DEFAULT 1,
                    submitted_by TEXT NOT NULL,
                    submitted_at TEXT NOT NULL,
                    opened_at TEXT,
                    UNIQUE(tender_id,vendor_id)
                );
                CREATE TABLE IF NOT EXISTS evaluations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bid_id INTEGER NOT NULL REFERENCES bids(id),
                    evaluation_round INTEGER NOT NULL,
                    evaluator TEXT NOT NULL,
                    criterion TEXT NOT NULL,
                    raw_value REAL NOT NULL,
                    score REAL NOT NULL,
                    comment TEXT NOT NULL DEFAULT '',
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(bid_id,evaluation_round,evaluator,criterion)
                );
                CREATE TABLE IF NOT EXISTS conflicts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tender_id INTEGER NOT NULL REFERENCES tenders(id),
                    evaluator TEXT NOT NULL,
                    vendor_id INTEGER REFERENCES vendors(id),
                    reason TEXT NOT NULL,
                    declared_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(tender_id,evaluator,vendor_id)
                );
                CREATE TABLE IF NOT EXISTS clarifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tender_id INTEGER NOT NULL REFERENCES tenders(id),
                    vendor_id INTEGER REFERENCES vendors(id),
                    question TEXT NOT NULL,
                    answer TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    answered_by TEXT,
                    created_at TEXT NOT NULL,
                    answered_at TEXT
                );
                CREATE TABLE IF NOT EXISTS complaints (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tender_id INTEGER NOT NULL REFERENCES tenders(id),
                    complainant TEXT NOT NULL,
                    body TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    resolution TEXT,
                    reviewed_by TEXT,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT
                );
                CREATE TABLE IF NOT EXISTS timeline (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tender_id INTEGER REFERENCES tenders(id),
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_bids_tender ON bids(tender_id,status);
                CREATE INDEX IF NOT EXISTS idx_eval_bid_round ON evaluations(bid_id,evaluation_round);

                CREATE TABLE IF NOT EXISTS contracts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    contract_no TEXT NOT NULL UNIQUE,
                    tender_id INTEGER NOT NULL UNIQUE REFERENCES tenders(id),
                    vendor_id INTEGER NOT NULL REFERENCES vendors(id),
                    title TEXT NOT NULL DEFAULT '',
                    total_amount_cents INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS contract_nodes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    contract_id INTEGER NOT NULL REFERENCES contracts(id),
                    seq INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    amount_cents INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(contract_id,seq)
                );
                CREATE TABLE IF NOT EXISTS node_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    node_id INTEGER NOT NULL REFERENCES contract_nodes(id),
                    quantity REAL NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    submitted_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS node_acceptances (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    node_id INTEGER NOT NULL REFERENCES contract_nodes(id),
                    report_id INTEGER REFERENCES node_reports(id),
                    amount_cents INTEGER NOT NULL,
                    expected_amount_cents INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'accepted',
                    note TEXT NOT NULL DEFAULT '',
                    accepted_by TEXT NOT NULL,
                    reviewed_by TEXT,
                    created_at TEXT NOT NULL,
                    reviewed_at TEXT,
                    version INTEGER NOT NULL DEFAULT 1
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_acceptance_active
                    ON node_acceptances(node_id) WHERE status IN ('review','accepted');
                CREATE TABLE IF NOT EXISTS contract_payables (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    contract_id INTEGER NOT NULL REFERENCES contracts(id),
                    node_id INTEGER NOT NULL REFERENCES contract_nodes(id),
                    acceptance_id INTEGER NOT NULL UNIQUE REFERENCES node_acceptances(id),
                    amount_cents INTEGER NOT NULL,
                    paid_cents INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'payable',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS contract_payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    contract_id INTEGER NOT NULL REFERENCES contracts(id),
                    payable_id INTEGER NOT NULL REFERENCES contract_payables(id),
                    amount_cents INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'posted',
                    note TEXT NOT NULL DEFAULT '',
                    registered_by TEXT NOT NULL,
                    reviewed_by TEXT,
                    created_at TEXT NOT NULL,
                    reviewed_at TEXT,
                    version INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS entity_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    version INTEGER NOT NULL,
                    snapshot TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(entity_type,entity_id,version)
                );
                CREATE INDEX IF NOT EXISTS idx_nodes_contract ON contract_nodes(contract_id,seq);
                CREATE INDEX IF NOT EXISTS idx_payables_contract ON contract_payables(contract_id);
                CREATE INDEX IF NOT EXISTS idx_payments_contract ON contract_payments(contract_id,status);
                """
            )

    def _audit(self, conn: sqlite3.Connection, tender_id: int | None, actor: str,
               action: str, details: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO timeline(tender_id,actor,action,details,created_at) VALUES(?,?,?,?,?)",
            (tender_id, actor, action, json.dumps(details, ensure_ascii=False, sort_keys=True), utcnow()),
        )

    def _tender(self, conn: sqlite3.Connection, tender_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM tenders WHERE id=?", (tender_id,)).fetchone()
        if not row:
            raise DomainError("采购项目不存在", 404)
        return row

    def create_vendor(self, actor: str, role: str, vendor_no: str, name: str,
                      representative: str) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"procurement", "supervisor"}, "创建供应商")
        if not vendor_no.strip() or not name.strip() or not representative.strip():
            raise DomainError("供应商编号、名称和代表不能为空")
        with self.connect() as conn:
            try:
                cur = conn.execute(
                    "INSERT INTO vendors(vendor_no,name,representative,created_at) VALUES(?,?,?,?)",
                    (vendor_no.strip(), name.strip(), representative.strip(), utcnow()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("供应商编号已存在", 409) from exc
            self._audit(conn, None, actor, "vendor.created", {"vendor_no": vendor_no.strip()})
            return dict(conn.execute("SELECT * FROM vendors WHERE id=?", (cur.lastrowid,)).fetchone())

    def create_tender(self, actor: str, role: str, tender_no: str, title: str,
                      deadline: str, criteria: list[dict[str, Any]], description: str = "") -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"procurement"}, "创建采购项目")
        parse_time(deadline)
        if not tender_no.strip() or not title.strip():
            raise DomainError("项目编号和标题不能为空")
        normalized_criteria = []
        total_weight = Decimal("0")
        for item in criteria:
            if not isinstance(item, dict) or not str(item.get("name", "")).strip():
                raise DomainError("评分项格式无效")
            kind = item.get("kind", "direct")
            if kind not in {"direct", "cost"}:
                raise DomainError("评分项类型只支持 direct 或 cost")
            try:
                weight = Decimal(str(item["weight"]))
                max_value = Decimal(str(item.get("max_value", 100)))
            except (KeyError, InvalidOperation) as exc:
                raise DomainError("评分权重或上限无效") from exc
            if weight <= 0 or max_value <= 0:
                raise DomainError("评分权重和上限必须大于0")
            total_weight += weight
            normalized_criteria.append({"name": str(item["name"]).strip(), "kind": kind,
                                        "weight": float(weight), "max_value": float(max_value)})
        if not normalized_criteria or total_weight != 100:
            raise DomainError("评分项权重合计必须等于100")
        with self.connect() as conn:
            try:
                cur = conn.execute(
                    """INSERT INTO tenders(tender_no,title,description,deadline,criteria,created_by,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (tender_no.strip(), title.strip(), description.strip(), parse_time(deadline).isoformat(timespec="seconds"),
                     json.dumps(normalized_criteria, ensure_ascii=False), actor, utcnow(), utcnow()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("项目编号已存在", 409) from exc
            self._audit(conn, cur.lastrowid, actor, "tender.created", {"tender_no": tender_no.strip()})
            return dict(self._tender(conn, cur.lastrowid))

    def publish_tender(self, actor: str, role: str, tender_id: int, expected_version: int) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"procurement"}, "发布采购项目")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            tender = self._tender(conn, tender_id)
            if tender["status"] != "draft":
                raise DomainError("只有草稿项目可以发布", 409)
            if tender["version"] != int(expected_version):
                raise DomainError("项目已变化，请刷新后重试", 409)
            conn.execute("UPDATE tenders SET status='published',version=version+1,updated_at=? WHERE id=?", (utcnow(), tender_id))
            self._audit(conn, tender_id, actor, "tender.published", {"deadline": tender["deadline"]})
            return dict(self._tender(conn, tender_id))

    def submit_bid(self, actor: str, role: str, tender_id: int, vendor_id: int,
                   payload: dict[str, Any], price: float, expected_version: int | None = None) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"vendor"}, "提交投标")
        if not isinstance(payload, dict):
            raise DomainError("投标内容必须是对象")
        try:
            price = float(price)
        except (TypeError, ValueError) as exc:
            raise DomainError("报价必须是数值") from exc
        if price <= 0:
            raise DomainError("报价必须大于0")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            tender = self._tender(conn, tender_id)
            if tender["status"] != "published":
                raise DomainError("当前项目不接受投标", 409)
            if datetime.now(timezone.utc) >= parse_time(tender["deadline"]):
                raise DomainError("投标截止时间已过", 409)
            vendor = conn.execute("SELECT * FROM vendors WHERE id=?", (vendor_id,)).fetchone()
            if not vendor:
                raise DomainError("供应商不存在", 404)
            if not conn.execute("SELECT 1 FROM conflicts WHERE tender_id=? AND vendor_id=? AND evaluator=?", (tender_id, vendor_id, actor)).fetchone():
                pass
            existing = conn.execute("SELECT * FROM bids WHERE tender_id=? AND vendor_id=?", (tender_id, vendor_id)).fetchone()
            payload_text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            digest = canonical_hash(payload)
            if existing:
                if existing["status"] != "sealed":
                    raise DomainError("投标已撤回或已开标，不能修改", 409)
                if expected_version is None or existing["version"] != int(expected_version):
                    raise DomainError("投标已变化，请刷新后重试", 409)
                conn.execute(
                    "UPDATE bids SET payload=?,payload_hash=?,price=?,version=version+1,submitted_at=? WHERE id=? AND version=?",
                    (payload_text, digest, price, utcnow(), existing["id"], expected_version),
                )
                bid_id = existing["id"]
                action = "bid.updated"
            else:
                cur = conn.execute(
                    """INSERT INTO bids(tender_id,vendor_id,payload,payload_hash,price,submitted_by,submitted_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (tender_id, vendor_id, payload_text, digest, price, actor, utcnow()),
                )
                bid_id = cur.lastrowid
                action = "bid.submitted"
            self._audit(conn, tender_id, actor, action, {"bid_id": bid_id, "vendor_id": vendor_id, "hash": digest})
            bid = dict(conn.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone())
            bid["payload_hash"] = digest
            return bid

    def withdraw_bid(self, actor: str, role: str, bid_id: int, expected_version: int) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"vendor"}, "撤回投标")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            bid = conn.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone()
            if not bid:
                raise DomainError("投标不存在", 404)
            tender = self._tender(conn, bid["tender_id"])
            if bid["submitted_by"] != actor:
                raise DomainError("只能撤回自己的投标", 403)
            if bid["version"] != int(expected_version):
                raise DomainError("投标已变化，请刷新后重试", 409)
            if datetime.now(timezone.utc) >= parse_time(tender["deadline"]) or bid["status"] != "sealed":
                raise DomainError("截止后不能撤回投标", 409)
            conn.execute("UPDATE bids SET status='withdrawn',version=version+1 WHERE id=?", (bid_id,))
            self._audit(conn, bid["tender_id"], actor, "bid.withdrawn", {"bid_id": bid_id})
            return dict(conn.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone())

    def open_bids(self, actor: str, role: str, tender_id: int, expected_version: int) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"procurement", "supervisor"}, "开标")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            tender = self._tender(conn, tender_id)
            if tender["status"] != "published":
                raise DomainError("项目当前不能开标", 409)
            if tender["version"] != int(expected_version):
                raise DomainError("项目已变化，请刷新后重试", 409)
            if datetime.now(timezone.utc) < parse_time(tender["deadline"]):
                raise DomainError("尚未到开标时间", 409)
            rows = conn.execute("SELECT * FROM bids WHERE tender_id=? AND status='sealed' ORDER BY id", (tender_id,)).fetchall()
            opened = []
            now = utcnow()
            for row in rows:
                digest = canonical_hash(json.loads(row["payload"]))
                if digest != row["payload_hash"]:
                    raise DomainError("投标完整性校验失败: %s" % row["id"], 409)
                conn.execute("UPDATE bids SET status='opened',opened_at=?,version=version+1 WHERE id=?", (now, row["id"]))
                opened.append(dict(conn.execute("SELECT * FROM bids WHERE id=?", (row["id"],)).fetchone()))
            conn.execute("UPDATE tenders SET status='opened',version=version+1,updated_at=? WHERE id=?", (now, tender_id))
            self._audit(conn, tender_id, actor, "tender.opened", {"bid_count": len(opened)})
            return {"tender": dict(self._tender(conn, tender_id)), "bids": opened}

    def declare_conflict(self, actor: str, role: str, tender_id: int, evaluator: str,
                         vendor_id: int | None, reason: str) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"evaluator", "procurement", "supervisor"}, "申报利益冲突")
        if not evaluator.strip() or not reason.strip():
            raise DomainError("评审人和冲突原因不能为空")
        with self.connect() as conn:
            self._tender(conn, tender_id)
            try:
                cur = conn.execute(
                    "INSERT INTO conflicts(tender_id,evaluator,vendor_id,reason,declared_by,created_at) VALUES(?,?,?,?,?,?)",
                    (tender_id, evaluator.strip(), vendor_id, reason.strip(), actor, utcnow()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("利益冲突已申报", 409) from exc
            self._audit(conn, tender_id, actor, "conflict.declared", {"evaluator": evaluator.strip(), "vendor_id": vendor_id, "reason": reason.strip()})
            return dict(conn.execute("SELECT * FROM conflicts WHERE id=?", (cur.lastrowid,)).fetchone())

    def evaluate_bid(self, actor: str, role: str, bid_id: int, values: dict[str, float],
                     comment: str = "") -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"evaluator"}, "评分")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            bid = conn.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone()
            if not bid:
                raise DomainError("投标不存在", 404)
            tender = self._tender(conn, bid["tender_id"])
            if tender["status"] not in {"opened", "reevaluation"} or tender["evaluations_locked"]:
                raise DomainError("当前项目不能评分", 409)
            if bid["status"] not in {"opened", "qualified"}:
                raise DomainError("该投标不能评分", 409)
            conflict = conn.execute(
                "SELECT 1 FROM conflicts WHERE tender_id=? AND evaluator=? AND (vendor_id=? OR vendor_id IS NULL)",
                (tender["id"], actor, bid["vendor_id"]),
            ).fetchone()
            if conflict:
                raise DomainError("评审人与该供应商存在利益冲突", 403)
            criteria = json.loads(tender["criteria"])
            missing = [c["name"] for c in criteria if c["name"] not in values]
            if missing:
                raise DomainError("缺少评分项: " + ",".join(missing))
            created = []
            now = utcnow()
            for criterion in criteria:
                try:
                    raw = float(values[criterion["name"]])
                except (TypeError, ValueError) as exc:
                    raise DomainError("评分值必须是数值") from exc
                if raw < 0 or raw > criterion["max_value"]:
                    raise DomainError("评分值超出范围: " + criterion["name"])
                if criterion["kind"] == "direct":
                    score = raw / criterion["max_value"] * 100
                else:
                    benchmark = criterion["max_value"]
                    score = min(100.0, benchmark / raw * 100) if raw > 0 else 0.0
                existing = conn.execute(
                    """SELECT * FROM evaluations WHERE bid_id=? AND evaluation_round=? AND evaluator=? AND criterion=?""",
                    (bid_id, tender["evaluation_round"], actor, criterion["name"]),
                ).fetchone()
                if existing:
                    raise DomainError("该评分项已提交，不能覆盖", 409)
                cur = conn.execute(
                    """INSERT INTO evaluations(bid_id,evaluation_round,evaluator,criterion,raw_value,score,comment,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (bid_id, tender["evaluation_round"], actor, criterion["name"], raw, score, comment.strip(), now, now),
                )
                created.append(dict(conn.execute("SELECT * FROM evaluations WHERE id=?", (cur.lastrowid,)).fetchone()))
            self._audit(conn, tender["id"], actor, "bid.evaluated", {"bid_id": bid_id, "criteria": [item["criterion"] for item in created]})
            return {"bid_id": bid_id, "evaluator": actor, "round": tender["evaluation_round"], "evaluations": created}

    def disqualify_bid(self, actor: str, role: str, bid_id: int, reason: str,
                       expected_version: int) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"procurement", "supervisor"}, "废标")
        if not reason.strip():
            raise DomainError("废标理由不能为空")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            bid = conn.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone()
            if not bid:
                raise DomainError("投标不存在", 404)
            if bid["version"] != int(expected_version):
                raise DomainError("投标已变化，请刷新后重试", 409)
            if bid["status"] not in {"opened", "qualified"}:
                raise DomainError("当前投标不能废标", 409)
            conn.execute("UPDATE bids SET status='disqualified',version=version+1 WHERE id=?", (bid_id,))
            self._audit(conn, bid["tender_id"], actor, "bid.disqualified", {"bid_id": bid_id, "reason": reason.strip()})
            return dict(conn.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone())

    def ask_clarification(self, actor: str, role: str, tender_id: int, vendor_id: int, question: str) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"vendor", "procurement", "supervisor"}, "提交澄清")
        if not question.strip():
            raise DomainError("澄清问题不能为空")
        with self.connect() as conn:
            self._tender(conn, tender_id)
            cur = conn.execute(
                "INSERT INTO clarifications(tender_id,vendor_id,question,created_at) VALUES(?,?,?,?)",
                (tender_id, vendor_id, question.strip(), utcnow()),
            )
            self._audit(conn, tender_id, actor, "clarification.asked", {"clarification_id": cur.lastrowid})
            return dict(conn.execute("SELECT * FROM clarifications WHERE id=?", (cur.lastrowid,)).fetchone())

    def answer_clarification(self, actor: str, role: str, clarification_id: int,
                             answer: str, publish: bool = True) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"procurement", "supervisor"}, "答复澄清")
        if not answer.strip():
            raise DomainError("澄清答复不能为空")
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM clarifications WHERE id=?", (clarification_id,)).fetchone()
            if not row:
                raise DomainError("澄清不存在", 404)
            if row["status"] != "pending":
                raise DomainError("澄清已经处理", 409)
            status = "published" if publish else "answered"
            conn.execute(
                "UPDATE clarifications SET answer=?,status=?,answered_by=?,answered_at=? WHERE id=?",
                (answer.strip(), status, actor, utcnow(), clarification_id),
            )
            self._audit(conn, row["tender_id"], actor, "clarification.answered", {"clarification_id": clarification_id, "published": publish})
            return dict(conn.execute("SELECT * FROM clarifications WHERE id=?", (clarification_id,)).fetchone())

    def submit_complaint(self, actor: str, role: str, tender_id: int, body: str) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"vendor", "evaluator", "procurement", "supervisor"}, "提交投诉")
        if not body.strip():
            raise DomainError("投诉内容不能为空")
        with self.connect() as conn:
            tender = self._tender(conn, tender_id)
            if tender["status"] in {"awarded", "cancelled"}:
                raise DomainError("项目已经结束，不能提交投诉", 409)
            cur = conn.execute(
                "INSERT INTO complaints(tender_id,complainant,body,created_at) VALUES(?,?,?,?)",
                (tender_id, actor, body.strip(), utcnow()),
            )
            self._audit(conn, tender_id, actor, "complaint.submitted", {"complaint_id": cur.lastrowid})
            return dict(conn.execute("SELECT * FROM complaints WHERE id=?", (cur.lastrowid,)).fetchone())

    def resolve_complaint(self, actor: str, role: str, complaint_id: int, decision: str,
                          resolution: str) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"supervisor"}, "处理投诉")
        if decision not in {"accepted", "rejected"} or not resolution.strip():
            raise DomainError("投诉决定或处理说明无效")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            complaint = conn.execute("SELECT * FROM complaints WHERE id=?", (complaint_id,)).fetchone()
            if not complaint:
                raise DomainError("投诉不存在", 404)
            if complaint["status"] != "open":
                raise DomainError("投诉已经处理", 409)
            conn.execute(
                "UPDATE complaints SET status=?,resolution=?,reviewed_by=?,resolved_at=? WHERE id=?",
                (decision, resolution.strip(), actor, utcnow(), complaint_id),
            )
            if decision == "accepted":
                tender = self._tender(conn, complaint["tender_id"])
                if tender["status"] in {"awarded", "cancelled"}:
                    raise DomainError("已结束项目不能重新评审", 409)
                conn.execute(
                    "UPDATE tenders SET status='reevaluation',evaluation_round=evaluation_round+1,evaluations_locked=0,version=version+1,updated_at=? WHERE id=?",
                    (utcnow(), tender["id"]),
                )
            self._audit(conn, complaint["tender_id"], actor, "complaint.resolved", {"complaint_id": complaint_id, "decision": decision})
            return dict(conn.execute("SELECT * FROM complaints WHERE id=?", (complaint_id,)).fetchone())

    def award_tender(self, actor: str, role: str, tender_id: int, expected_version: int) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"supervisor"}, "授标")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            tender = self._tender(conn, tender_id)
            if tender["status"] not in {"opened", "reevaluation"}:
                raise DomainError("当前项目不能授标", 409)
            if tender["version"] != int(expected_version):
                raise DomainError("项目已变化，请刷新后重试", 409)
            open_complaint = conn.execute("SELECT COUNT(*) AS c FROM complaints WHERE tender_id=? AND status='open'", (tender_id,)).fetchone()["c"]
            if open_complaint:
                raise DomainError("存在未处理投诉，不能授标", 409)
            bids = conn.execute("SELECT * FROM bids WHERE tender_id=? AND status IN ('opened','qualified')", (tender_id,)).fetchall()
            criteria = json.loads(tender["criteria"])
            expected_criteria = {c["name"] for c in criteria}
            ranking = []
            for bid in bids:
                rows = conn.execute(
                    "SELECT criterion,AVG(score) AS score FROM evaluations WHERE bid_id=? AND evaluation_round=? GROUP BY criterion",
                    (bid["id"], tender["evaluation_round"]),
                ).fetchall()
                scores = {row["criterion"]: row["score"] for row in rows}
                if set(scores) != expected_criteria:
                    raise DomainError("投标尚未完成全部评分: %s" % bid["id"], 409)
                weighted = 0.0
                for criterion in criteria:
                    weighted += scores[criterion["name"]] * criterion["weight"] / 100
                ranking.append({"bid_id": bid["id"], "vendor_id": bid["vendor_id"], "price": bid["price"], "score": round(weighted, 2)})
            if not ranking:
                raise DomainError("没有可授标的有效投标", 409)
            ranking.sort(key=lambda item: (-item["score"], item["price"], item["bid_id"]))
            winner = ranking[0]
            snapshot = {"tender_id": tender_id, "round": tender["evaluation_round"], "ranking": ranking, "winner": winner, "awarded_by": actor, "awarded_at": utcnow()}
            conn.execute(
                "UPDATE tenders SET status='awarded',awarded_bid_id=?,award_snapshot=?,evaluations_locked=1,version=version+1,updated_at=? WHERE id=? AND version=?",
                (winner["bid_id"], json.dumps(snapshot, ensure_ascii=False), utcnow(), tender_id, expected_version),
            )
            conn.execute("UPDATE bids SET status='awarded',version=version+1 WHERE id=?", (winner["bid_id"],))
            self._audit(conn, tender_id, actor, "tender.awarded", {"winner": winner, "ranking": ranking})
            return {"tender": dict(self._tender(conn, tender_id)), "award": snapshot}

    # ---- 履约验收 ----

    @staticmethod
    def money_to_cents(value: Any, field: str = "金额") -> int:
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, TypeError) as exc:
            raise DomainError("%s必须是数值" % field) from exc
        if not amount.is_finite() or amount <= 0:
            raise DomainError("%s必须大于0" % field)
        cents = (amount * 100).quantize(Decimal("1"))
        if cents != amount * 100:
            raise DomainError("%s最多两位小数" % field)
        return int(cents)

    @staticmethod
    def yuan(cents: int) -> float:
        return round(cents / 100, 2)

    def _snapshot(self, conn: sqlite3.Connection, entity_type: str, entity_id: int,
                  version: int, actor: str, snapshot: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO entity_versions(entity_type,entity_id,version,snapshot,actor,created_at) VALUES(?,?,?,?,?,?)",
            (entity_type, entity_id, version, json.dumps(snapshot, ensure_ascii=False, sort_keys=True, default=str), actor, utcnow()),
        )

    def _contract(self, conn: sqlite3.Connection, contract_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM contracts WHERE id=?", (contract_id,)).fetchone()
        if not row:
            raise DomainError("合同不存在", 404)
        return row

    @staticmethod
    def _serialize_contract(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"], "contract_no": row["contract_no"], "tender_id": row["tender_id"],
            "vendor_id": row["vendor_id"], "title": row["title"],
            "total_amount": ProcurementService.yuan(row["total_amount_cents"]),
            "status": row["status"], "version": row["version"],
            "created_by": row["created_by"], "created_at": row["created_at"], "updated_at": row["updated_at"],
        }

    @staticmethod
    def _serialize_node(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"], "contract_id": row["contract_id"], "seq": row["seq"], "name": row["name"],
            "amount": ProcurementService.yuan(row["amount_cents"]), "status": row["status"],
            "version": row["version"], "accepted_amount": ProcurementService.yuan(row["accepted_cents"]),
            "paid_amount": ProcurementService.yuan(row["paid_cents"]),
        }

    def create_contract(self, actor: str, role: str, tender_id: int, contract_no: str,
                        nodes: list[dict[str, Any]], title: str = "") -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"supervisor"}, "创建合同")
        contract_no = contract_no.strip()
        if not contract_no:
            raise DomainError("合同编号不能为空")
        if not isinstance(nodes, list) or not nodes:
            raise DomainError("至少拆分一个履约节点")
        prepared = []
        total = 0
        seen = set()
        for index, item in enumerate(nodes, start=1):
            if not isinstance(item, dict) or not str(item.get("name", "")).strip():
                raise DomainError("节点名称不能为空")
            cents = self.money_to_cents(item.get("amount"), "节点金额")
            seq = int(item.get("seq", index))
            if seq in seen:
                raise DomainError("节点序号不能重复")
            seen.add(seq)
            prepared.append({"seq": seq, "name": str(item["name"]).strip(), "amount_cents": cents})
            total += cents
        prepared.sort(key=lambda n: n["seq"])
        now = utcnow()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            tender = self._tender(conn, tender_id)
            if tender["status"] != "awarded" or not tender["awarded_bid_id"]:
                raise DomainError("只能从已授标项目创建合同", 409)
            bid = conn.execute("SELECT * FROM bids WHERE id=?", (tender["awarded_bid_id"],)).fetchone()
            if total != int(bid["price"] * 100 + 0.5):
                raise DomainError("节点金额合计必须与中标金额一致（%s）" % self.yuan(int(bid["price"] * 100 + 0.5)))
            try:
                cur = conn.execute(
                    """INSERT INTO contracts(contract_no,tender_id,vendor_id,title,total_amount_cents,created_by,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (contract_no, tender_id, bid["vendor_id"], title.strip() or tender["title"], total, actor, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("合同编号或授标项目合同已存在", 409) from exc
            contract_id = cur.lastrowid
            for node in prepared:
                conn.execute(
                    "INSERT INTO contract_nodes(contract_id,seq,name,amount_cents,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                    (contract_id, node["seq"], node["name"], node["amount_cents"], now, now),
                )
            self._audit(conn, tender_id, actor, "contract.created",
                        {"contract_id": contract_id, "contract_no": contract_no, "nodes": len(prepared)})
            result = self._load_contract(conn, contract_id, role, actor)
            self._snapshot(conn, "contract", contract_id, 1, actor, result["contract"])
            return result

    def report_completion(self, actor: str, role: str, node_id: int, quantity: float,
                          note: str = "") -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"vendor"}, "报完成量")
        try:
            quantity = float(quantity)
        except (TypeError, ValueError) as exc:
            raise DomainError("完成比例必须是数值") from exc
        if quantity <= 0 or quantity > 100:
            raise DomainError("完成比例必须在0到100之间")
        now = utcnow()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            node = conn.execute("SELECT * FROM contract_nodes WHERE id=?", (node_id,)).fetchone()
            if not node:
                raise DomainError("履约节点不存在", 404)
            contract = self._contract(conn, node["contract_id"])
            if contract["status"] != "active":
                raise DomainError("合同已结束，不能报送", 409)
            if not self._actor_owns_contract(conn, contract, actor):
                raise DomainError("只能为本单位中标的合同报送完成量", 403)
            if node["status"] == "done":
                raise DomainError("该节点已完成，不能重复报送", 409)
            cur = conn.execute(
                "INSERT INTO node_reports(node_id,quantity,note,submitted_by,created_at) VALUES(?,?,?,?,?)",
                (node_id, quantity, note.strip(), actor, now),
            )
            conn.execute("UPDATE contract_nodes SET status='reported',version=version+1,updated_at=? WHERE id=?", (now, node_id))
            self._audit(conn, contract["tender_id"], actor, "node.reported",
                        {"node_id": node_id, "report_id": cur.lastrowid, "quantity": quantity})
            new_node = conn.execute("SELECT * FROM contract_nodes WHERE id=?", (node_id,)).fetchone()
            self._snapshot(conn, "node", node_id, new_node["version"], actor, self._node_view(conn, new_node))
            return self._load_contract(conn, contract["id"], role, actor)

    def accept_completion(self, actor: str, role: str, node_id: int, amount: float,
                          report_id: int | None = None, note: str = "") -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"procurement"}, "履约验收")
        cents = self.money_to_cents(amount, "验收金额")
        now = utcnow()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            node = conn.execute("SELECT * FROM contract_nodes WHERE id=?", (node_id,)).fetchone()
            if not node:
                raise DomainError("履约节点不存在", 404)
            contract = self._contract(conn, node["contract_id"])
            if contract["status"] != "active":
                raise DomainError("合同已结束，不能验收", 409)
            if node["status"] not in {"reported", "partial"}:
                raise DomainError("节点尚无完成量报送或已完成", 409)
            report = None
            if report_id is not None:
                report = conn.execute("SELECT * FROM node_reports WHERE id=? AND node_id=?", (report_id, node_id)).fetchone()
                if not report:
                    raise DomainError("完成量报送不存在", 404)
            else:
                report = conn.execute(
                    "SELECT * FROM node_reports WHERE node_id=? ORDER BY id DESC LIMIT 1", (node_id,)
                ).fetchone()
                if not report:
                    raise DomainError("节点尚无完成量报送", 409)
            # 同一节点只保留一条有效验收（部分唯一索引兜底，并发重复提交返回冲突）
            if conn.execute(
                "SELECT 1 FROM node_acceptances WHERE node_id=? AND status IN ('review','accepted')", (node_id,)
            ).fetchone():
                raise DomainError("该节点已有有效验收，重复提交冲突", 409)
            accepted_before = conn.execute("SELECT COALESCE(SUM(amount_cents),0) AS c FROM node_acceptances WHERE node_id=? AND status='accepted'", (node_id,)).fetchone()["c"]
            if accepted_before + cents > node["amount_cents"]:
                raise DomainError("节点累计验收不能超过节点金额（剩余可验收 %s）" % self.yuan(node["amount_cents"] - accepted_before), 409)
            contract_accepted = conn.execute(
                "SELECT COALESCE(SUM(a.amount_cents),0) AS c FROM node_acceptances a JOIN contract_nodes n ON n.id=a.node_id WHERE n.contract_id=? AND a.status='accepted'",
                (contract["id"],),
            ).fetchone()["c"]
            if contract_accepted + cents > contract["total_amount_cents"]:
                raise DomainError("合同累计验收不能超过合同额", 409)
            expected = int(node["amount_cents"] * Decimal(str(report["quantity"])) / 100)
            # 金额不匹配：进入待复核，不形成应付
            status = "accepted" if cents == expected else "review"
            try:
                cur = conn.execute(
                    """INSERT INTO node_acceptances(node_id,report_id,amount_cents,expected_amount_cents,status,note,accepted_by,created_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (node_id, report["id"], cents, expected, status, note.strip(), actor, now),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("该节点已有有效验收，重复提交冲突", 409) from exc
            acceptance_id = cur.lastrowid
            if status == "accepted":
                self._post_acceptance(conn, node, contract, acceptance_id, cents, now)
            self._audit(conn, contract["tender_id"], actor, "node.accepted",
                        {"node_id": node_id, "acceptance_id": acceptance_id,
                         "amount": self.yuan(cents), "expected": self.yuan(expected), "status": status})
            self._snapshot(conn, "acceptance", acceptance_id, 1, actor,
                           dict(conn.execute("SELECT * FROM node_acceptances WHERE id=?", (acceptance_id,)).fetchone()))
            return self._load_contract(conn, contract["id"], role, actor)

    def _post_acceptance(self, conn: sqlite3.Connection, node: sqlite3.Row, contract: sqlite3.Row,
                         acceptance_id: int, cents: int, now: str) -> None:
        conn.execute(
            """INSERT INTO contract_payables(contract_id,node_id,acceptance_id,amount_cents,created_at,updated_at)
               VALUES(?,?,?,?,?,?)""",
            (contract["id"], node["id"], acceptance_id, cents, now, now),
        )
        node_status = "partial"
        accepted_total = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) AS c FROM node_acceptances WHERE node_id=? AND status='accepted'", (node["id"],)
        ).fetchone()["c"]
        if accepted_total >= node["amount_cents"]:
            node_status = "done"
        conn.execute("UPDATE contract_nodes SET status=?,version=version+1,updated_at=? WHERE id=?", (node_status, now, node["id"]))
        new_node = conn.execute("SELECT * FROM contract_nodes WHERE id=?", (node["id"],)).fetchone()
        self._snapshot(conn, "node", node["id"], new_node["version"], "system", self._node_view(conn, new_node))

    def resolve_acceptance(self, actor: str, role: str, acceptance_id: int,
                           decision: str, amount: float | None = None, note: str = "") -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"supervisor"}, "复核验收")
        if decision not in {"approve", "reject"}:
            raise DomainError("复核决定只支持 approve 或 reject")
        now = utcnow()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM node_acceptances WHERE id=?", (acceptance_id,)).fetchone()
            if not row:
                raise DomainError("验收记录不存在", 404)
            if row["status"] != "review":
                raise DomainError("该验收不在待复核状态", 409)
            node = conn.execute("SELECT * FROM contract_nodes WHERE id=?", (row["node_id"],)).fetchone()
            contract = self._contract(conn, node["contract_id"])
            if decision == "reject":
                conn.execute(
                    "UPDATE node_acceptances SET status='rejected',reviewed_by=?,reviewed_at=?,note=?,version=version+1 WHERE id=?",
                    (actor, now, (note.strip() or row["note"]), acceptance_id),
                )
                self._audit(conn, contract["tender_id"], actor, "acceptance.rejected", {"acceptance_id": acceptance_id})
            else:
                cents = self.money_to_cents(amount, "确认金额") if amount is not None else row["amount_cents"]
                accepted_before = conn.execute(
                    "SELECT COALESCE(SUM(amount_cents),0) AS c FROM node_acceptances WHERE node_id=? AND status='accepted'", (node["id"],)
                ).fetchone()["c"]
                if accepted_before + cents > node["amount_cents"]:
                    raise DomainError("确认金额超过节点剩余可验收金额", 409)
                contract_accepted = conn.execute(
                    "SELECT COALESCE(SUM(a.amount_cents),0) AS c FROM node_acceptances a JOIN contract_nodes n ON n.id=a.node_id WHERE n.contract_id=? AND a.status='accepted'",
                    (contract["id"],),
                ).fetchone()["c"]
                if contract_accepted + cents > contract["total_amount_cents"]:
                    raise DomainError("确认后合同累计验收超过合同额", 409)
                conn.execute(
                    "UPDATE node_acceptances SET status='accepted',amount_cents=?,reviewed_by=?,reviewed_at=?,note=?,version=version+1 WHERE id=?",
                    (cents, actor, now, (note.strip() or row["note"]), acceptance_id),
                )
                self._post_acceptance(conn, node, contract, acceptance_id, cents, now)
                self._audit(conn, contract["tender_id"], actor, "acceptance.approved",
                            {"acceptance_id": acceptance_id, "amount": self.yuan(cents)})
            updated = conn.execute("SELECT * FROM node_acceptances WHERE id=?", (acceptance_id,)).fetchone()
            self._snapshot(conn, "acceptance", acceptance_id, updated["version"], actor, dict(updated))
            return self._load_contract(conn, contract["id"], role, actor)

    def register_payment(self, actor: str, role: str, payable_id: int, amount: float,
                         note: str = "") -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"supervisor"}, "登记付款")
        cents = self.money_to_cents(amount, "付款金额")
        now = utcnow()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            payable = conn.execute("SELECT * FROM contract_payables WHERE id=?", (payable_id,)).fetchone()
            if not payable:
                raise DomainError("应付记录不存在", 404)
            if payable["status"] == "paid":
                raise DomainError("该应付记录已结清", 409)
            node = conn.execute("SELECT * FROM contract_nodes WHERE id=?", (payable["node_id"],)).fetchone()
            contract = self._contract(conn, payable["contract_id"])
            remaining_payable = payable["amount_cents"] - payable["paid_cents"]
            contract_paid = conn.execute(
                "SELECT COALESCE(SUM(CASE WHEN status='posted' THEN amount_cents ELSE 0 END),0) AS c FROM contract_payments WHERE contract_id=?",
                (contract["id"],),
            ).fetchone()["c"]
            node_paid = conn.execute(
                "SELECT COALESCE(SUM(CASE WHEN status='posted' THEN amount_cents ELSE 0 END),0) AS c FROM contract_payments WHERE contract_id=? AND payable_id IN (SELECT id FROM contract_payables WHERE node_id=?)",
                (contract["id"], node["id"]),
            ).fetchone()["c"]
            node_accepted = conn.execute(
                "SELECT COALESCE(SUM(amount_cents),0) AS c FROM node_acceptances WHERE node_id=? AND status='accepted'", (node["id"],)
            ).fetchone()["c"]
            # 节点超付 / 超过合同额：停在待复核，付款不生效
            over = cents > remaining_payable or node_paid + cents > node_accepted or contract_paid + cents > contract["total_amount_cents"]
            status = "review" if over else "posted"
            cur = conn.execute(
                """INSERT INTO contract_payments(contract_id,payable_id,amount_cents,status,note,registered_by,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (contract["id"], payable_id, cents, status, note.strip(), actor, now),
            )
            payment_id = cur.lastrowid
            if status == "posted":
                self._apply_payment(conn, payable, cents, now)
            self._audit(conn, contract["tender_id"], actor, "payment.registered",
                        {"payable_id": payable_id, "payment_id": payment_id,
                         "amount": self.yuan(cents), "status": status})
            result = self._load_contract(conn, contract["id"], role, actor)
            self._snapshot(conn, "payment", payment_id, 1, actor,
                           dict(conn.execute("SELECT * FROM contract_payments WHERE id=?", (payment_id,)).fetchone()))
            return result

    def _apply_payment(self, conn: sqlite3.Connection, payable: sqlite3.Row, cents: int, now: str) -> None:
        new_paid = payable["paid_cents"] + cents
        payable_status = "paid" if new_paid >= payable["amount_cents"] else "partial"
        conn.execute("UPDATE contract_payables SET paid_cents=?,status=?,updated_at=? WHERE id=?",
                     (new_paid, payable_status, now, payable["id"]))
        contract_id = payable["contract_id"]
        pending_payables = conn.execute(
            "SELECT COUNT(*) AS c FROM contract_payables WHERE contract_id=? AND status!='paid'", (contract_id,)
        ).fetchone()["c"]
        unfinished_nodes = conn.execute(
            "SELECT COUNT(*) AS c FROM contract_nodes WHERE contract_id=? AND status!='done'", (contract_id,)
        ).fetchone()["c"]
        if not pending_payables and not unfinished_nodes:
            conn.execute("UPDATE contracts SET status='completed',version=version+1,updated_at=? WHERE id=?", (now, contract_id))
            contract = conn.execute("SELECT * FROM contracts WHERE id=?", (contract_id,)).fetchone()
            self._snapshot(conn, "contract", contract_id, contract["version"], "system", self._serialize_contract(contract))

    def resolve_payment(self, actor: str, role: str, payment_id: int,
                        decision: str, amount: float | None = None, note: str = "") -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"supervisor"}, "复核付款")
        if decision not in {"approve", "reject"}:
            raise DomainError("复核决定只支持 approve 或 reject")
        now = utcnow()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            payment = conn.execute("SELECT * FROM contract_payments WHERE id=?", (payment_id,)).fetchone()
            if not payment:
                raise DomainError("付款记录不存在", 404)
            if payment["status"] != "review":
                raise DomainError("该付款不在待复核状态", 409)
            contract = self._contract(conn, payment["contract_id"])
            if decision == "reject":
                conn.execute(
                    "UPDATE contract_payments SET status='rejected',reviewed_by=?,reviewed_at=?,note=?,version=version+1 WHERE id=?",
                    (actor, now, (note.strip() or payment["note"]), payment_id),
                )
                self._audit(conn, contract["tender_id"], actor, "payment.rejected", {"payment_id": payment_id})
            else:
                cents = self.money_to_cents(amount, "确认金额") if amount is not None else payment["amount_cents"]
                payable = conn.execute("SELECT * FROM contract_payables WHERE id=?", (payment["payable_id"],)).fetchone()
                remaining_payable = payable["amount_cents"] - payable["paid_cents"]
                contract_paid = conn.execute(
                    "SELECT COALESCE(SUM(CASE WHEN status='posted' THEN amount_cents ELSE 0 END),0) AS c FROM contract_payments WHERE contract_id=?",
                    (contract["id"],),
                ).fetchone()["c"]
                if cents > remaining_payable or contract_paid + cents > contract["total_amount_cents"]:
                    raise DomainError("确认金额仍超出可付额度，不能通过复核", 409)
                conn.execute(
                    "UPDATE contract_payments SET status='posted',amount_cents=?,reviewed_by=?,reviewed_at=?,note=?,version=version+1 WHERE id=?",
                    (cents, actor, now, (note.strip() or payment["note"]), payment_id),
                )
                self._apply_payment(conn, payable, cents, now)
                self._audit(conn, contract["tender_id"], actor, "payment.approved",
                            {"payment_id": payment_id, "amount": self.yuan(cents)})
            updated = conn.execute("SELECT * FROM contract_payments WHERE id=?", (payment_id,)).fetchone()
            self._snapshot(conn, "payment", payment_id, updated["version"], actor, dict(updated))
            return self._load_contract(conn, contract["id"], role, actor)

    def _node_view(self, conn: sqlite3.Connection, node: sqlite3.Row) -> dict[str, Any]:
        accepted_cents = conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN status='accepted' THEN amount_cents ELSE 0 END),0) AS c FROM node_acceptances WHERE node_id=?",
            (node["id"],),
        ).fetchone()["c"]
        paid_cents = conn.execute(
            """SELECT COALESCE(SUM(CASE WHEN p.status='posted' THEN p.amount_cents ELSE 0 END),0) AS c
               FROM contract_payments p JOIN contract_payables py ON py.id=p.payable_id WHERE py.node_id=?""",
            (node["id"],),
        ).fetchone()["c"]
        data = dict(node)
        return {
            "id": data["id"], "contract_id": data["contract_id"], "seq": data["seq"], "name": data["name"],
            "amount": self.yuan(data["amount_cents"]), "status": data["status"], "version": data["version"],
            "accepted_amount": self.yuan(accepted_cents), "paid_amount": self.yuan(paid_cents),
        }

    def _load_contract(self, conn: sqlite3.Connection, contract_id: int, role: str, actor: str) -> dict[str, Any]:
        row = self._contract(conn, contract_id)
        contract = self._serialize_contract(row)
        node_rows = conn.execute("SELECT * FROM contract_nodes WHERE contract_id=? ORDER BY seq", (contract_id,)).fetchall()
        nodes = [self._node_view(conn, n) for n in node_rows]
        reports = [dict(r) for r in conn.execute(
            "SELECT id,node_id,quantity,note,submitted_by,created_at FROM node_reports WHERE node_id IN (SELECT id FROM contract_nodes WHERE contract_id=?) ORDER BY id",
            (contract_id,),
        ).fetchall()]
        acceptances = []
        for r in conn.execute(
            """SELECT a.* FROM node_acceptances a JOIN contract_nodes n ON n.id=a.node_id
               WHERE n.contract_id=? ORDER BY a.id""",
            (contract_id,),
        ).fetchall():
            item = dict(r)
            item["amount"] = self.yuan(item.pop("amount_cents"))
            item["expected_amount"] = self.yuan(item.pop("expected_amount_cents"))
            acceptances.append(item)
        payables, payments = [], []
        for r in conn.execute("SELECT * FROM contract_payables WHERE contract_id=? ORDER BY id", (contract_id,)).fetchall():
            item = dict(r)
            item["amount"] = self.yuan(item.pop("amount_cents"))
            item["paid"] = self.yuan(item.pop("paid_cents"))
            payables.append(item)
        for r in conn.execute("SELECT * FROM contract_payments WHERE contract_id=? ORDER BY id", (contract_id,)).fetchall():
            item = dict(r)
            item["amount"] = self.yuan(item.pop("amount_cents"))
            payments.append(item)
        accepted_total = self.yuan(sum(int(round(n["accepted_amount"] * 100)) for n in nodes))
        paid_total = self.yuan(sum(int(round(n["paid_amount"] * 100)) for n in nodes))
        # 公开视图：只看履约状态，不暴露金额明细
        if role == "public" or (role == "vendor" and not self._actor_owns_contract(conn, row, actor)):
            public_nodes = [{"id": n["id"], "seq": n["seq"], "name": n["name"], "status": n["status"]} for n in nodes]
            public_acceptances = [{"id": a["id"], "node_id": a["node_id"], "status": a["status"], "created_at": a["created_at"]} for a in acceptances]
            return {
                "contract": {"id": contract["id"], "contract_no": contract["contract_no"], "tender_id": contract["tender_id"],
                             "vendor_id": contract["vendor_id"], "title": contract["title"], "status": contract["status"]},
                "nodes": public_nodes, "acceptances": public_acceptances,
                "payables": [], "payments": [], "reports": [],
            }
        if role == "vendor":
            reports = [r for r in reports if r["submitted_by"] == actor]
        return {
            "contract": contract, "nodes": nodes, "reports": reports,
            "acceptances": acceptances, "payables": payables, "payments": payments,
            "accepted_total": accepted_total, "paid_total": paid_total,
        }

    @staticmethod
    def _actor_owns_contract(conn: sqlite3.Connection, contract: sqlite3.Row, actor: str) -> bool:
        row = conn.execute(
            """SELECT 1 FROM bids b WHERE b.id=(SELECT awarded_bid_id FROM tenders WHERE id=?)
               AND b.vendor_id=? AND EXISTS (SELECT 1 FROM bids x WHERE x.id=b.id AND x.submitted_by=?)""",
            (contract["tender_id"], contract["vendor_id"], actor),
        ).fetchone()
        return row is not None

    def get_contract(self, actor: str, role: str, contract_id: int) -> dict[str, Any]:
        with self.connect() as conn:
            row = self._contract(conn, contract_id)
            if role == "vendor" and not self._actor_owns_contract(conn, row, actor):
                raise DomainError("合同不存在", 404)
            return self._load_contract(conn, contract_id, role, actor)

    def contract_overview(self, actor: str, role: str) -> dict[str, Any]:
        with self.connect() as conn:
            contracts = [self._load_contract(conn, r["id"], role, actor)["contract"]
                         for r in conn.execute("SELECT id FROM contracts ORDER BY id DESC").fetchall()]
            todos = self._todos(conn, role, actor)
        return {"contracts": contracts, "todos": todos, "role": role}

    def _todos(self, conn: sqlite3.Connection, role: str, actor: str) -> list[dict[str, Any]]:
        todos: list[dict[str, Any]] = []
        if role == "supervisor":
            for r in conn.execute(
                """SELECT a.id,a.node_id,n.contract_id,a.amount_cents,a.expected_amount_cents FROM node_acceptances a
                   JOIN contract_nodes n ON n.id=a.node_id WHERE a.status='review' ORDER BY a.id"""
            ).fetchall():
                todos.append({"type": "acceptance_review", "acceptance_id": r["id"], "node_id": r["node_id"],
                              "contract_id": r["contract_id"], "amount": self.yuan(r["amount_cents"]),
                              "expected_amount": self.yuan(r["expected_amount_cents"])})
            for r in conn.execute(
                """SELECT p.id,p.payable_id,p.contract_id,p.amount_cents FROM contract_payments p WHERE p.status='review' ORDER BY p.id"""
            ).fetchall():
                todos.append({"type": "payment_review", "payment_id": r["id"], "payable_id": r["payable_id"],
                              "contract_id": r["contract_id"], "amount": self.yuan(r["amount_cents"])})
        elif role == "procurement":
            for r in conn.execute(
                """SELECT n.id,n.contract_id,n.seq,n.name FROM contract_nodes n JOIN contracts c ON c.id=n.contract_id
                   WHERE n.status='reported' AND c.status='active'
                   AND NOT EXISTS (SELECT 1 FROM node_acceptances a WHERE a.node_id=n.id AND a.status IN ('review','accepted'))
                   ORDER BY n.id"""
            ).fetchall():
                todos.append({"type": "acceptance_pending", "node_id": r["id"], "contract_id": r["contract_id"],
                              "seq": r["seq"], "name": r["name"]})
        elif role == "vendor":
            for r in conn.execute(
                """SELECT n.id,n.contract_id,n.seq,n.name FROM contract_nodes n JOIN contracts c ON c.id=n.contract_id
                   JOIN tenders t ON t.id=c.tender_id JOIN bids b ON b.id=t.awarded_bid_id
                   WHERE b.submitted_by=? AND n.status='pending' AND c.status='active' ORDER BY n.seq""",
                (actor,),
            ).fetchall():
                todos.append({"type": "report_pending", "node_id": r["id"], "contract_id": r["contract_id"],
                              "seq": r["seq"], "name": r["name"]})
        return todos

    def list_versions(self, actor: str, role: str, entity_type: str, entity_id: int) -> dict[str, Any]:
        require_role(role, {"supervisor", "auditor", "procurement"}, "查看版本历史")
        if entity_type not in {"contract", "node", "acceptance", "payment"}:
            raise DomainError("版本对象类型无效")
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id,entity_type,entity_id,version,actor,created_at,snapshot FROM entity_versions WHERE entity_type=? AND entity_id=? ORDER BY version",
                (entity_type, entity_id),
            ).fetchall()
            items = []
            for r in rows:
                item = dict(r)
                item["snapshot"] = json.loads(item["snapshot"])
                items.append(item)
        return {"entity_type": entity_type, "entity_id": entity_id, "versions": items}

    def get_tender(self, actor: str, role: str, tender_id: int) -> dict[str, Any]:
        with self.connect() as conn:
            tender = dict(self._tender(conn, tender_id))
            bids = []
            if role in {"procurement", "supervisor", "auditor"} and tender["status"] in {"opened", "reevaluation", "awarded"}:
                bids = [dict(r) for r in conn.execute("SELECT * FROM bids WHERE tender_id=? ORDER BY id", (tender_id,)).fetchall()]
            elif role == "vendor":
                bids = []
                for row in conn.execute(
                    "SELECT b.*,t.status AS tender_status FROM bids b JOIN tenders t ON t.id=b.tender_id WHERE b.tender_id=? AND b.submitted_by=?",
                    (tender_id, actor),
                ).fetchall():
                    item = dict(row)
                    item.pop("tender_status", None)
                    if tender["status"] not in {"opened", "reevaluation", "awarded"}:
                        item.pop("payload", None)
                    bids.append(item)
            else:
                bids = [dict(r) for r in conn.execute(
                    "SELECT id,tender_id,vendor_id,price,status,payload_hash,submitted_at,opened_at FROM bids WHERE tender_id=? ORDER BY id",
                    (tender_id,),
                ).fetchall()]
            clarifications = [dict(r) for r in conn.execute(
                "SELECT id,tender_id,vendor_id,question,answer,status,answered_at FROM clarifications WHERE tender_id=? AND status='published' ORDER BY id",
                (tender_id,),
            ).fetchall()]
            return {"tender": tender, "bids": bids, "clarifications": clarifications}

    def state(self, actor: str = "", role: str = "public") -> dict[str, Any]:
        with self.connect() as conn:
            tenders = [dict(r) for r in conn.execute(
                "SELECT id,tender_no,title,description,status,deadline,evaluation_round,version,awarded_bid_id,created_at,updated_at FROM tenders ORDER BY id DESC"
            ).fetchall()]
            timeline = [dict(r) for r in conn.execute("SELECT * FROM timeline ORDER BY id DESC LIMIT 200").fetchall()]
            if role in {"procurement", "supervisor", "auditor"}:
                bids = [dict(r) for r in conn.execute(
                    """SELECT b.id,b.tender_id,b.vendor_id,b.price,b.status,b.payload_hash,b.submitted_at,b.opened_at,
                              CASE WHEN t.status IN ('opened','reevaluation','awarded') THEN b.payload ELSE NULL END AS payload
                       FROM bids b JOIN tenders t ON t.id=b.tender_id ORDER BY b.id DESC LIMIT 200"""
                ).fetchall()]
                complaints = [dict(r) for r in conn.execute("SELECT * FROM complaints ORDER BY id DESC LIMIT 100").fetchall()]
            elif role == "vendor":
                bids = []
                for row in conn.execute(
                    """SELECT b.*,t.status AS tender_status FROM bids b JOIN tenders t ON t.id=b.tender_id
                       WHERE b.submitted_by=? ORDER BY b.id DESC LIMIT 100""",
                    (actor,),
                ).fetchall():
                    item = dict(row)
                    status = item.pop("tender_status")
                    if status not in {"opened", "reevaluation", "awarded"}:
                        item.pop("payload", None)
                    bids.append(item)
                complaints = [dict(r) for r in conn.execute(
                    "SELECT * FROM complaints WHERE complainant=? ORDER BY id DESC LIMIT 100", (actor,)
                ).fetchall()]
            else:
                bids, complaints = [], []
            overview = self.contract_overview(actor, role)
        return {"tenders": tenders, "bids": bids, "complaints": complaints, "timeline": timeline,
                "role": role, "performance": overview}

    def seed_demo(self) -> dict[str, Any]:
        with self.connect() as conn:
            if conn.execute("SELECT COUNT(*) AS c FROM tenders").fetchone()["c"]:
                return {"seeded": False, "reason": "已有数据"}
        vendor = self.create_vendor("proc-demo", "procurement", "V-001", "启明科技", "vendor-demo")
        deadline = (datetime.now(timezone.utc) + __import__("datetime").timedelta(hours=1)).isoformat(timespec="seconds")
        tender = self.create_tender(
            "proc-demo", "procurement", "TENDER-DEMO", "服务器采购", deadline,
            [{"name": "价格", "weight": 60, "kind": "cost", "max_value": 1000000},
             {"name": "质量", "weight": 40, "kind": "direct", "max_value": 100}],
        )
        published = self.publish_tender("proc-demo", "procurement", tender["id"], tender["version"])
        self.submit_bid("vendor-demo", "vendor", tender["id"], vendor["id"], {"价格": 900000, "质量": 90}, 900000)
        return {"seeded": True, "tender_id": tender["id"], "vendor_id": vendor["id"], "published_version": published["version"]}


class ApiHandler(BaseHTTPRequestHandler):
    service: ProcurementService

    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _headers(self) -> tuple[str, str]:
        return self.headers.get("X-User", ""), self.headers.get("X-Role", "public")

    def _json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 2_000_000:
            raise DomainError("请求体过大", 413)
        if not length:
            return {}
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DomainError("请求体不是有效 JSON") from exc
        if not isinstance(value, dict):
            raise DomainError("JSON 请求体必须是对象")
        return value

    def do_GET(self) -> None:
        try:
            path = urlparse(self.path).path
            if path in {"/", "/index.html"}:
                body = (ROOT / "static" / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path in {"/performance", "/performance.html"}:
                body = (ROOT / "static" / "performance.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            actor, role = self._headers()
            if path == "/health":
                self._send(200, {"status": "ok", "service": "public-procurement"})
            elif path == "/api/state":
                self._send(200, self.service.state(actor, role))
            elif path == "/api/contracts/overview":
                self._send(200, self.service.contract_overview(actor, role))
            elif path.startswith("/api/contracts/"):
                self._send(200, self.service.get_contract(actor, role, int(path.split("/")[3])))
            elif path.startswith("/api/versions/"):
                parts = path.split("/")
                self._send(200, self.service.list_versions(actor, role, parts[3], int(parts[4])))
            elif path.startswith("/api/tenders/"):
                self._send(200, self.service.get_tender(actor, role, int(path.split("/")[3])))
            else:
                self._send(404, {"error": "接口不存在"})
        except DomainError as exc:
            self._send(exc.status, {"error": str(exc)})
        except (ValueError, IndexError) as exc:
            self._send(400, {"error": str(exc)})

    def do_POST(self) -> None:
        try:
            path, data, (actor, role) = urlparse(self.path).path, self._json(), self._headers()
            if path == "/api/vendors":
                result = self.service.create_vendor(actor, role, **data)
            elif path == "/api/tenders":
                result = self.service.create_tender(actor, role, **data)
            elif path == "/api/tenders/publish":
                result = self.service.publish_tender(actor, role, **data)
            elif path == "/api/bids":
                result = self.service.submit_bid(actor, role, **data)
            elif path == "/api/bids/withdraw":
                result = self.service.withdraw_bid(actor, role, **data)
            elif path == "/api/tenders/open":
                result = self.service.open_bids(actor, role, **data)
            elif path == "/api/conflicts":
                result = self.service.declare_conflict(actor, role, **data)
            elif path == "/api/evaluations":
                result = self.service.evaluate_bid(actor, role, **data)
            elif path == "/api/bids/disqualify":
                result = self.service.disqualify_bid(actor, role, **data)
            elif path == "/api/clarifications":
                result = self.service.ask_clarification(actor, role, **data)
            elif path == "/api/clarifications/answer":
                result = self.service.answer_clarification(actor, role, **data)
            elif path == "/api/complaints":
                result = self.service.submit_complaint(actor, role, **data)
            elif path == "/api/complaints/resolve":
                result = self.service.resolve_complaint(actor, role, **data)
            elif path == "/api/tenders/award":
                result = self.service.award_tender(actor, role, **data)
            elif path == "/api/contracts":
                result = self.service.create_contract(actor, role, **data)
            elif path == "/api/performance/reports":
                result = self.service.report_completion(actor, role, **data)
            elif path == "/api/performance/acceptances":
                result = self.service.accept_completion(actor, role, **data)
            elif path == "/api/performance/acceptances/resolve":
                result = self.service.resolve_acceptance(actor, role, **data)
            elif path == "/api/performance/payments":
                result = self.service.register_payment(actor, role, **data)
            elif path == "/api/performance/payments/resolve":
                result = self.service.resolve_payment(actor, role, **data)
            else:
                raise DomainError("接口不存在", 404)
            self._send(201, result)
        except DomainError as exc:
            self._send(exc.status, {"error": str(exc)})
        except (KeyError, TypeError, ValueError) as exc:
            self._send(400, {"error": "请求参数错误: %s" % exc})
        except Exception as exc:
            self._send(500, {"error": "服务器内部错误", "detail": str(exc)})

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def serve(service: ProcurementService, host: str, port: int) -> None:
    ApiHandler.service = service
    server = ThreadingHTTPServer((host, port), ApiHandler)
    print("Public procurement service listening on http://%s:%s" % (host, port))
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="公共采购密封投标服务")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8209)
    parser.add_argument("--init", action="store_true")
    parser.add_argument("--seed", action="store_true")
    args = parser.parse_args()
    service = ProcurementService(args.db)
    if args.init:
        print(json.dumps(service.seed_demo() if args.seed else {"initialized": True, "db": args.db}, ensure_ascii=False))
        return
    serve(service, args.host, args.port)


if __name__ == "__main__":
    main()
