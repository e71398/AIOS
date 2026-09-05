#!/usr/bin/env python3
"""
AIOS v4.0 电脑端快捷入口脚本
功能：通过终端快速向 AIOS 发送任务
用法：python3 aios_quick.py "你的任务描述"
"""

import sys
import json
import uuid
import os
from datetime import datetime
from pathlib import Path

AIOS_HOME = os.environ.get("AIOS_HOME", "${AIOS_HOME}")
SESSION_FILE = f"{AIOS_HOME}/cache/.session_state"


class AIOSQuickCommand:
    def __init__(self):
        self.aios_home = AIOS_HOME

    def generate_task_id(self):
        return str(uuid.uuid4())

    def load_session_context(self):
        """加载上一次的会话上下文，确保任务接续"""
        try:
            with open(SESSION_FILE, 'r') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {
                'last_task_id': None,
                'active_contexts': []
            }

    def save_session_context(self, session_data):
        """保存当前会话状态"""
        Path(SESSION_FILE).parent.mkdir(parents=True, exist_ok=True)
        with open(SESSION_FILE, 'w') as f:
            json.dump(session_data, f, indent=2)

    def submit_task(self, user_input):
        """提交任务到 AIOS 任务队列"""

        session = self.load_session_context()
        task_id = self.generate_task_id()

        # 构建任务对象
        task_payload = {
            'Task_ID': task_id,
            'Task_Type': self.detect_task_type(user_input),
            'Priority': self.detect_priority(user_input),
            'Context_Summary': user_input[:512],
            'Data_Pointer_UUID': None,
            'Constraints': {
                'max_cost_usd': 10.0,
                'max_duration_minutes': 30,
                'safety_level': self.detect_safety_level(user_input)
            },
            'Verification_Criteria': [],
            'Created_By': 'aios-quick-command',
            'Created_At': datetime.now().isoformat(),
            'Parent_Task_ID': session.get('last_task_id'),
            'Session_ID': str(uuid.uuid4())
        }

        # 添加历史上下文引用
        if session.get('active_contexts'):
            task_payload['Historical_Context_Refs'] = session['active_contexts'][-3:]

        # 保存任务到队列（通过文件系统）
        task_file = Path(self.aios_home) / 'logs' / 'task_logs' / f"task_{task_id}.json"
        task_file.parent.mkdir(parents=True, exist_ok=True)
        task_file.write_text(json.dumps(task_payload, indent=2, ensure_ascii=False))

        print(f"\n📤 任务已提交")
        print(f"   Task ID: {task_id}")
        print(f"   类型: {task_payload['Task_Type']}")
        print(f"   优先级: {task_payload['Priority']}")
        print(f"   任务文件: {task_file}")

        # 更新会话状态
        session['last_task_id'] = task_id
        session['active_contexts'].append({
            'task_id': task_id,
            'summary': user_input[:100],
            'timestamp': datetime.now().isoformat()
        })
        session['active_contexts'] = session['active_contexts'][-10:]
        self.save_session_context(session)

        return task_payload

    def detect_task_type(self, user_input):
        """自动识别任务类型"""
        keywords = {
            'coding': ['写代码', '代码', '编程', 'python', '脚本', '重构', 'code', 'program'],
            'plc_logic': ['PLC', '变频', '控制逻辑', '工控', '电气', 'plc'],
            'research': ['分析', '调研', '查', '研究', '对比', 'research', 'analyze'],
            'document': ['文档', '报告', '整理', '总结', 'doc', 'report'],
            'verification': ['测试', '验证', '检查', 'test', 'verify']
        }

        for task_type, kws in keywords.items():
            if any(kw.lower() in user_input.lower() for kw in kws):
                return task_type
        return 'general'

    def detect_priority(self, user_input):
        """自动识别优先级"""
        urgent_keywords = ['紧急', '马上', '立刻', '快', '急', 'urgent']
        high_keywords = ['重要', '关键', '优先', 'important', 'high']

        if any(kw in user_input for kw in urgent_keywords):
            return 'P0_URGENT'
        elif any(kw in user_input for kw in high_keywords):
            return 'P1_HIGH'
        return 'P2_NORMAL'

    def detect_safety_level(self, user_input):
        """自动识别安全等级"""
        dangerous_keywords = ['PLC', '控制', '硬件', '电气', '物理', '马达']
        return 'L0' if any(kw in user_input for kw in dangerous_keywords) else 'L3'


def main():
    print("=" * 50)
    print("AIOS v4.0 快捷命令")
    print("=" * 50)

    if len(sys.argv) < 2:
        print("用法:")
        print('  python3 aios_quick.py "你的任务描述"')
        print("\n示例:")
        print('  python3 aios_quick.py "分析当前目录的PLC逻辑"')
        print('  python3 aios_quick.py "写一个处理CSV的Python脚本"')
        print('  python3 aios_quick.py "紧急：检查2号站控制程序"')
        print("=" * 50)

        client = AIOSQuickCommand()
        session = client.load_session_context()
        if session.get('last_task_id'):
            print(f"\n📋 最近任务: {session['last_task_id']}")
        if session.get('active_contexts'):
            print("📜 会话历史:")
            for ctx in session['active_contexts'][-3:]:
                print(f"   - [{ctx['timestamp'][:10]}] {ctx['summary'][:60]}")
        return

    user_input = ' '.join(sys.argv[1:])
    print(f"输入: {user_input}")
    print("=" * 50)

    client = AIOSQuickCommand()
    result = client.submit_task(user_input)

    if result:
        print("\n🕐 任务已入队。")
        print(f"   查看: cat {AIOS_HOME}/logs/task_logs/task_{result['Task_ID']}.json")


if __name__ == '__main__':
    main()
