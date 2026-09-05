#!/usr/bin/env python3
"""
AIOS v4.0 世界模型自动校准器
功能：当执行结果与预测不符时，自动修正世界模型参数
触发：Hermes 慢循环每晚自动调用
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

AIOS_HOME = os.environ.get("AIOS_HOME", "${AIOS_HOME}")


class WorldModelCalibrator:
    def __init__(self):
        self.reports_dir = Path(AIOS_HOME) / "logs" / "world_model_reports"
        self.calibration_dir = Path(AIOS_HOME) / "knowledge" / "calibration_reports"
        self.rules_path = Path(AIOS_HOME) / "kernel" / "config" / "physics_rules.yaml"
        self.mismatch_threshold = 0.3

    def calibrate(self):
        """执行校准流程"""
        print(f"\n🔧 世界模型校准开始...")
        print(f"   检查时间范围: 最近 7 天\n")

        predictions = self.collect_prediction_data(days=7)

        if not predictions:
            print("   未发现需要校准的数据")
            return {"status": "NO_DATA"}

        errors = self.analyze_errors(predictions)
        calibrations = self.generate_calibration(errors)
        report = self.create_report(predictions, errors, calibrations)
        self.save_report(report)

        return report

    def collect_prediction_data(self, days=7):
        """收集预测数据"""
        predictions = []
        if not self.reports_dir.exists():
            return predictions

        for report_file in self.reports_dir.glob("*.json"):
            try:
                with open(report_file, 'r') as f:
                    data = json.load(f)

                timestamp_str = data.get('timestamp', '')
                if timestamp_str:
                    timestamp = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                    age_days = (datetime.now() - timestamp.replace(tzinfo=None)).days
                    if age_days <= days:
                        predictions.append(data)
            except Exception:
                continue

        return predictions

    def analyze_errors(self, predictions):
        """分析误差模式"""
        errors = {
            "plc_timing": {"total": 0, "mismatch": 0},
            "voltage_range": {"total": 0, "mismatch": 0},
            "logic_safety": {"total": 0, "mismatch": 0},
            "cost_prediction": {"total": 0, "mismatch": 0}
        }

        for pred in predictions:
            for check in pred.get('checks', []):
                check_name = check.get('check', 'unknown')
                for category in errors:
                    if category in check_name:
                        errors[category]['total'] += 1
                        if not check.get('passed', True):
                            errors[category]['mismatch'] += 1

        return errors

    def generate_calibration(self, errors):
        """生成校准建议"""
        calibrations = []
        for category, data in errors.items():
            if data['total'] > 0:
                mismatch_rate = data['mismatch'] / data['total']
                if mismatch_rate > self.mismatch_threshold:
                    calibrations.append({
                        "category": category,
                        "mismatch_rate": round(mismatch_rate, 3),
                        "recommendation": f"建议调整 {category} 参数，误差率 {mismatch_rate:.1%}",
                        "priority": "HIGH" if mismatch_rate > 0.5 else "MEDIUM"
                    })

        return calibrations

    def create_report(self, predictions, errors, calibrations):
        """创建校准报告"""
        return {
            "calibration_id": f"cal_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
            "timestamp": datetime.now().isoformat(),
            "predictions_analyzed": len(predictions),
            "error_breakdown": errors,
            "calibrations": calibrations,
            "status": "REQUIRES_REVIEW" if calibrations else "OPTIMAL"
        }

    def save_report(self, report):
        """保存校准报告"""
        self.calibration_dir.mkdir(parents=True, exist_ok=True)
        report_path = self.calibration_dir / f"{report['calibration_id']}.json"
        with open(report_path, 'w') as f:
            json.dump(report, f, indent=2, ensure_ascii=False)

        print(f"   ✅ 校准报告已保存: {report_path}")
        print(f"   状态: {report['status']}")

        if report.get('calibrations'):
            print(f"   发现 {len(report['calibrations'])} 项需要调整:")
            for c in report['calibrations']:
                print(f"   - [{c['priority']}] {c['recommendation']}")


if __name__ == '__main__':
    calibrator = WorldModelCalibrator()
    result = calibrator.calibrate()
