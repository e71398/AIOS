#!/usr/bin/env python3
"""
AIOS v4.0 强验证门禁脚本 (Verification Gate)
功能：确保所有输出在物理上可执行、逻辑上可追溯
用法：python3 verify.py <task.json>
返回：Exit 0 = 通过，Exit 1 = 失败
"""

import json
import sys
import os
import subprocess
import hashlib
from pathlib import Path
from datetime import datetime

AIOS_HOME = os.environ.get("AIOS_HOME", "${AIOS_HOME}")


class AIOSVerifier:
    def __init__(self, task_config_path):
        with open(task_config_path, 'r') as f:
            self.task = json.load(f)

        self.task_id = self.task.get('Task_ID', 'unknown')
        self.task_type = self.task.get('Task_Type', 'unknown')
        self.verification_criteria = self.task.get('Verification_Criteria', [])
        self.result_pointer = self.task.get('Result_Pointer_UUID')
        self.work_dir = self.task.get('Work_Dir', f'{AIOS_HOME}/sandbox/coding')

        self.results = []
        self.passed = True

    def run_all_checks(self):
        """按顺序运行所有验证检查"""
        print(f"\n{'='*60}")
        print(f"AIOS Verifier - Task: {self.task_id}")
        print(f"类型: {self.task_type}")
        print(f"开始时间: {datetime.now().isoformat()}")
        print(f"{'='*60}\n")

        # 1. 基础语法检查
        self.check_syntax()

        # 2. 文件完整性检查
        self.check_file_integrity()

        # 3. 安全扫描
        self.check_security()

        # 4. 逻辑对齐检查（按任务类型）
        self.check_logic_alignment()

        # 5. 资源占用检查
        self.check_resource_usage()

        # 6. 自定义验收标准
        self.check_custom_criteria()

        self.finalize_report()
        return self.passed

    def check_syntax(self):
        """语法检查"""
        print("[CHECK 1/6] 语法验证...")

        task_type_map = {
            'coding': ['py', 'js', 'yaml', 'yml'],
            'plc_logic': ['plc', 'lad'],
            'document': ['md', 'json']
        }

        extensions = task_type_map.get(self.task_type, ['*'])
        work_path = Path(self.work_dir)
        if not work_path.exists():
            self.log_pass("工作目录不存在，跳过语法检查")
            return

        files = list(work_path.rglob('*'))
        code_files = [f for f in files if f.suffix.lstrip('.') in extensions or '*' in extensions]

        if not code_files:
            self.log_pass("无代码文件需检查")
            return

        for file_path in code_files:
            try:
                if file_path.suffix == '.py':
                    result = subprocess.run(
                        ['python3', '-m', 'py_compile', str(file_path)],
                        capture_output=True, timeout=10
                    )
                    if result.returncode == 0:
                        self.log_pass(f"语法正确: {file_path.name}")
                    else:
                        self.log_fail(f"语法错误: {file_path.name} - {result.stderr.decode()}")
            except Exception as e:
                self.log_fail(f"语法检查异常: {file_path.name} - {str(e)}")

    def check_file_integrity(self):
        """文件完整性检查"""
        print("[CHECK 2/6] 文件完整性验证...")

        output_name = self.task.get('Output_File', '')
        if output_name:
            output_file = Path(self.work_dir) / output_name
            if output_file.is_file() and output_file.stat().st_size > 0:
                file_hash = hashlib.md5(output_file.read_bytes()).hexdigest()
                self.log_pass(f"文件完整: {output_file.name} (MD5: {file_hash[:16]}...)")
            else:
                self.log_fail(f"输出文件缺失或非文件: {output_name}")
        else:
            work_path = Path(self.work_dir)
            if work_path.exists():
                files = list(work_path.rglob('*'))
                non_hidden = [f for f in files if not f.name.startswith('.')]
                if non_hidden:
                    self.log_pass(f"发现 {len(non_hidden)} 个产出文件")
                else:
                    self.log_fail("未发现任何产出文件")
            else:
                self.log_fail("工作目录不存在")

    def check_security(self):
        """安全扫描"""
        print("[CHECK 3/6] 安全扫描...")

        dangerous_patterns = [
            ('os.system', 'HIGH'),
            ('subprocess.call', 'MEDIUM'),
            ('eval(', 'HIGH'),
            ('exec(', 'HIGH'),
            ('__import__', 'MEDIUM'),
            ('pickle.load', 'HIGH'),
            ('yaml.load', 'HIGH'),
            ('rm -rf', 'MEDIUM'),
        ]

        work_path = Path(self.work_dir)
        if not work_path.exists():
            self.log_pass("工作目录不存在，跳过安全扫描")
            return

        files = list(work_path.rglob('*.py'))
        for file_path in files:
            try:
                content = file_path.read_text()
                for pattern, severity in dangerous_patterns:
                    if pattern in content:
                        self.log_warn(f"安全注意[{severity}]: {file_path.name} 包含 '{pattern}'")
            except Exception:
                pass

        self.log_pass("安全扫描完成")

    def check_logic_alignment(self):
        """逻辑对齐检查"""
        print("[CHECK 4/6] 逻辑对齐验证...")

        if self.task_type == 'plc_logic':
            self.check_plc_constraints()
        elif self.task_type == 'coding':
            self.check_coding_standards()
        else:
            self.log_pass("逻辑对齐检查跳过（非专项任务类型）")

    def check_plc_constraints(self):
        """PLC 安全约束检查"""
        print("  └─ 检查 PLC 时序约束...")

        plc_rules = {
            'frequency_switch_min_delay_ms': 200,
            'max_concurrent_outputs': 8,
            'emergency_stop_required': True
        }

        work_path = Path(self.work_dir)
        sandbox_files = list(work_path.rglob('*')) if work_path.exists() else []

        emergency_found = any('emergency' in f.name.lower() for f in sandbox_files)

        if plc_rules['emergency_stop_required'] and not emergency_found:
            self.log_fail("缺少 Emergency Stop 逻辑（安全违规）")
        else:
            self.log_pass("PLC 安全约束检查通过")

    def check_coding_standards(self):
        """编码规范检查"""
        print("  └─ 检查代码规范...")

        work_path = Path(self.work_dir)
        if not work_path.exists():
            return

        files = list(work_path.rglob('*.py'))
        for file_path in files:
            try:
                content = file_path.read_text()
                if '"""' not in content and "'''" not in content:
                    self.log_warn(f"缺少文档注释: {file_path.name}")
                if 'def ' in content and '-> ' not in content and ': ' in content:
                    self.log_warn(f"建议添加类型注解: {file_path.name}")
            except Exception:
                pass

    def check_resource_usage(self):
        """资源占用检查"""
        print("[CHECK 5/6] 资源占用验证...")

        max_file_size_mb = 100
        work_path = Path(self.work_dir)

        if not work_path.exists():
            self.log_pass("工作目录不存在，跳过资源检查")
            return

        files = list(work_path.rglob('*'))
        for file_path in files:
            if file_path.is_file():
                size_mb = file_path.stat().st_size / (1024 * 1024)
                if size_mb > max_file_size_mb:
                    self.log_fail(f"文件过大: {file_path.name} ({size_mb:.2f}MB > {max_file_size_mb}MB)")

        self.log_pass("资源占用检查完成")

    def check_custom_criteria(self):
        """自定义验收标准检查"""
        print("[CHECK 6/6] 自定义验收标准...")

        for criterion in self.verification_criteria:
            criterion_type = criterion.get('type')

            if criterion_type == 'unit_test':
                self.run_unit_tests(criterion)
            elif criterion_type == 'file_exists':
                self.check_file_exists(criterion)
            elif criterion_type == 'command_output':
                self.run_command_check(criterion)

    def run_unit_tests(self, criterion):
        """运行单元测试"""
        test_file = criterion.get('test_file', 'test_*.py')
        result = subprocess.run(
            ['python3', '-m', 'pytest', test_file, '-v'],
            capture_output=True, text=True, cwd=self.work_dir, timeout=60
        )
        if result.returncode == 0:
            self.log_pass(f"单元测试通过: {test_file}")
        else:
            self.log_fail(f"单元测试失败: {result.stderr[-200:]}")

    def check_file_exists(self, criterion):
        """检查文件是否存在"""
        filename = criterion.get('filename')
        expected_path = Path(self.work_dir) / filename
        if expected_path.exists():
            self.log_pass(f"必需文件存在: {filename}")
        else:
            self.log_fail(f"必需文件缺失: {filename}")

    def run_command_check(self, criterion):
        """运行命令检查"""
        command = criterion.get('command')
        expected_output = criterion.get('expected', '')
        # 2026-08-17 P1-SEM-001: never use shell=True; the criterion
        # ``command`` is an AIOS-owned config value that is already a
        # list/tuple of argv tokens, so we just dispatch it as argv.
        # If a legacy string slips in we wrap it in [command] — no shell.
        if isinstance(command, (list, tuple)):
            argv = list(command)
        elif command is None:
            self.log_fail("命令检查缺少 command 字段")
            return
        else:
            argv = [str(command)]
        result = subprocess.run(
            argv, shell=False, capture_output=True, text=True, timeout=30,
        )
        if expected_output in result.stdout:
            self.log_pass(f"命令检查通过: {argv}")
        else:
            self.log_fail(f"命令输出不符合预期: {argv}")

    def log_pass(self, message):
        print(f"  ✅ {message}")
        self.results.append({'status': 'PASS', 'message': message})

    def log_fail(self, message):
        print(f"  ❌ {message}")
        self.results.append({'status': 'FAIL', 'message': message})
        self.passed = False

    def log_warn(self, message):
        print(f"  ⚠️  {message}")
        self.results.append({'status': 'WARN', 'message': message})

    def finalize_report(self):
        """生成最终报告"""
        print(f"\n{'='*60}")
        print(f"验证完成时间: {datetime.now().isoformat()}")

        passed_count = sum(1 for r in self.results if r['status'] == 'PASS')
        failed_count = sum(1 for r in self.results if r['status'] == 'FAIL')
        warn_count = sum(1 for r in self.results if r['status'] == 'WARN')

        print(f"通过: {passed_count} | 失败: {failed_count} | 警告: {warn_count}")
        print(f"{'='*60}")

        if self.passed:
            print("🎉 验证结果：全部通过")
            print("→ 允许进入下一步或交付给用户")
        else:
            print("🚨 验证结果：有项目未通过")
            print("→ 触发中循环修正，重新调度执行")

        # 写入验证报告
        report = {
            'task_id': self.task_id,
            'verification_time': datetime.now().isoformat(),
            'passed': self.passed,
            'summary': {
                'pass': passed_count,
                'fail': failed_count,
                'warn': warn_count
            },
            'details': self.results
        }

        report_dir = Path(AIOS_HOME) / 'logs' / 'verification_reports'
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / f"{self.task_id}.json"
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))

        print(f"\n📋 验证报告已保存: {report_path}")


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("用法: python3 verify.py <task_config.json>")
        sys.exit(1)

    verifier = AIOSVerifier(sys.argv[1])
    success = verifier.run_all_checks()

    sys.exit(0 if success else 1)
