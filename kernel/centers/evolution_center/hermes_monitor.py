"""
Hermes Monitor - AIOS 主动监督核心模块
========================================
其他中心/AI完成任务或发生事件时主动调用 Hermes，不是被动等报告。
"""
import os, json
from datetime import datetime
from typing import Dict, List

CORE_RULES = {
    "authorization": "只做分配给你的任务，不抢做别人的",
    "risk": "高风险操作必须确认授权",
    "state": "已完成的任务不重复执行",
    "collaboration": "只做自己被分配的部分，交接要说清楚",
    "honesty": "不隐瞒问题，不确定要如实说"
}

VIOLATION_LEVELS = {
    "CRITICAL": ["unauthorized_execution", "risk_violation"],
    "HIGH": ["skipped_pre_check", "state_violation", "collaboration_violation"],
    "MEDIUM": ["single_solution_complex", "incomplete_handshake"],
    "LOW": ["minor_delayed_report", "skipped_optional_step"]
}

class HermesMonitor:
    def __init__(self):
        self.log_path = "${AIOS_HOME}/logs/hermes/"
        self.violation_log = f"{self.log_path}violations.jsonl"
        self.compliance_db = f"{self.log_path}compliance_db.json"
        self.agent_stats = f"{self.log_path}agent_stats.json"
        self.agent_registry = f"{self.log_path}agent_registry.json"
        os.makedirs(self.log_path, exist_ok=True)
        self._init_storage()

    def register_agent(self, agent_id: str, agent_type: str = "ai", metadata: dict = None):
        """动态注册Agent — 新增AI自动加入监督."""
        reg = self._load_json(self.agent_registry)
        reg[agent_id] = {"type": agent_type, "registered_at": datetime.now().isoformat(),
                         "metadata": metadata or {}}
        self._save_json(self.agent_registry, reg)
        self._update_stats(agent_id, "registered", 0)

    def get_registered_agents(self) -> list:
        return list(self._load_json(self.agent_registry).keys())

    def _is_registered(self, agent_id: str) -> bool:
        """自动注册未知agent — 不再硬编码."""
        reg = self._load_json(self.agent_registry)
        if agent_id not in reg:
            self.register_agent(agent_id)
        return True

    def _init_storage(self):
        for p, default in [(self.compliance_db, {"records":[]}), (self.agent_stats, {}), (self.agent_registry, {})]:
            if not os.path.exists(p): self._save_json(p, default)

    def _save_json(self, path, data):
        with open(path, 'w') as f: json.dump(data, f, indent=2, ensure_ascii=False)

    def _load_json(self, path):
        with open(path, 'r') as f: return json.load(f)

    def on_event(self, event_type: str, event_data: dict):
        handlers = {"task_completed": self._handle_task_completed,
            "violation_detected": self._handle_violation}
        if event_type in handlers: handlers[event_type](event_data)

    def on_task_completed(self, task_data: dict):
        self._handle_task_completed(task_data)

    def on_violation_detected(self, violation_data: dict):
        self._handle_violation(violation_data)

    def _handle_task_completed(self, task_data: dict):
        agent = task_data.get("agent_id","unknown")
        tid = task_data.get("task_id","unknown")
        if agent == "unknown": return
        self._is_registered(agent)  # 自动注册, 不再硬编码
        violations = self._analyze_compliance(task_data)
        record = {"timestamp": datetime.now().isoformat(), "event":"task_completed",
            "task_id": tid, "agent_id": agent, "violations": violations}
        db = self._load_json(self.compliance_db)
        db["records"].append(record)
        if len(db["records"]) > 1000: db["records"] = db["records"][-1000:]
        self._save_json(self.compliance_db, db)
        self._update_stats(agent, "task_completed", len(violations))
        if violations: self._trigger_alert(agent, tid, violations)

    def _handle_violation(self, v: dict):
        v["timestamp"] = datetime.now().isoformat()
        v["severity"] = self._classify_severity(v.get("type",""))
        with open(self.violation_log, 'a') as f:
            f.write(json.dumps(v, ensure_ascii=False) + "\n")
        self._update_stats(v.get("agent_id","?"), "violation", 1)

    def _analyze_compliance(self, td: dict) -> list:
        v = []
        if not td.get("pre_check_passed", True):
            v.append({"rule":"pre_check","type":"skipped_pre_check","severity":"HIGH"})
        if td.get("task_type") in ("complex","pipeline") and td.get("solution_count",1)==1:
            v.append({"rule":"multi_solution","type":"single_solution_complex","severity":"MEDIUM"})
        return v

    def _classify_severity(self, vt: str) -> str:
        for lv, types in VIOLATION_LEVELS.items():
            if vt in types: return lv
        return "MEDIUM"

    def _update_stats(self, agent: str, evt: str, vc: int):
        s = self._load_json(self.agent_stats)
        if agent not in s: s[agent] = {"total_tasks":0,"total_violations":0}
        if evt == "task_completed": s[agent]["total_tasks"] += 1
        if evt == "violation" or vc > 0: s[agent]["total_violations"] += vc
        self._save_json(self.agent_stats, s)

    def _trigger_alert(self, agent, tid, violations):
        high = [v for v in violations if v.get("severity") in ("CRITICAL","HIGH")]
        if high:
            alert = {"level":"HIGH","agent":agent,"task":tid,"count":len(high),
                     "ts":datetime.now().isoformat()}
            with open(f"{self.log_path}alerts.jsonl", 'a') as f:
                f.write(json.dumps(alert, ensure_ascii=False)+"\n")

    def get_compliance_report(self) -> dict:
        s = self._load_json(self.agent_stats)
        total_t = sum(x.get("total_tasks",0) for x in s.values())
        total_v = sum(x.get("total_violations",0) for x in s.values())
        return {"total_tasks":total_t,"total_violations":total_v,"agents":s,
                "rate":round((total_t-total_v)/max(total_t,1),4)}

    def get_agent_compliance(self, agent: str) -> dict:
        s = self._load_json(self.agent_stats).get(agent,{})
        t = s.get("total_tasks",0); v = s.get("total_violations",0)
        return {"agent":agent,"tasks":t,"violations":v,"rate":round((t-v)/max(t,1),4)}

    def get_violations(self, agent: str = None, limit: int = 50) -> list:
        vs = []
        if os.path.exists(self.violation_log):
            with open(self.violation_log) as f:
                for line in f:
                    try:
                        v = json.loads(line.strip())
                        if not agent or v.get("agent_id")==agent: vs.append(v)
                    except: pass
        return vs[-limit:]
