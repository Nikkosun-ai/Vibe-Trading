"""策略文档解析器 — 上传 PDF/Markdown → AI 提取 → 结构化配置."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)
STRATEGIES_DIR = Path(__file__).resolve().parent.parent / "strategies"


def parse_strategy_document(file_path: str) -> dict:
    file_path = Path(file_path)
    if not file_path.exists():
        return {"success": False, "error": f"文件不存在: {file_path}"}

    try:
        if file_path.suffix.lower() in (".md", ".markdown", ".txt"):
            content = file_path.read_text(encoding="utf-8")
        elif file_path.suffix.lower() == ".pdf":
            content = _read_pdf(file_path)
        else:
            return {"success": False, "error": f"不支持的文件格式: {file_path.suffix}"}
    except Exception as e:
        return {"success": False, "error": f"文件读取失败: {e}"}

    if not content or len(content.strip()) < 100:
        return {"success": False, "error": "文档内容过短，无法提取策略"}

    try:
        config = _ai_parse_strategy(content)
    except Exception as e:
        return {"success": False, "error": f"AI解析失败: {e}"}

    required = ["id", "name", "sub_strategies"]
    for field in required:
        if field not in config:
            return {"success": False, "error": f"策略配置缺少必需字段: {field}"}

    config_path = STRATEGIES_DIR / f"{config['id']}.json"
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

    return {"success": True, "config": config, "saved_to": str(config_path)}


def _read_pdf(file_path: Path) -> str:
    try:
        import pypdfium2
        pdf = pypdfium2.PdfDocument(str(file_path))
        pages = [pdf[i].get_text() for i in range(len(pdf))]
        return "\n".join(pages)
    except ImportError:
        import subprocess
        result = subprocess.run(
            ["python", "-c",
             f"import PyPDF2; r=PyPDF2.PdfReader('{file_path}'); "
             "print('\\n'.join(p.extract_text() or '' for p in r.pages))"],
            capture_output=True, text=True, timeout=30)
        return result.stdout


def _ai_parse_strategy(content: str) -> dict:
    prompt = f"""从以下策略文档中提取结构化配置。返回严格JSON：

{{
  "id": "策略英文ID（kebab-case）",
  "name": "策略中文名称",
  "version": "版本号",
  "description": "一句话描述",
  "scan_time": "14:30",
  "layers": {{
    "trend_quality": {{"thresholds": {{"strong": 65, "weak": 50}}, "factors": [{{"name":"","weight":0}}]}},
    "strategy_fit": {{"threshold": 55, "factors": [{{"name":"","weight":0}}]}},
    "entry_timing": {{"factors": [{{"name":"","weight":0}}]}}
  }},
  "sub_strategies": [
    {{"name": "", "label": "", "entry": "", "exit": "",
      "min_hold_days": 0, "cooldown_days": 0, "position_weight": 1.0}}
  ],
  "risk": {{"position_cap": 0.25, "surge_stop_5d": 0.15}},
  "display": {{
    "pool_groups": [{{"strategy": "", "label": "", "color": "#hex"}}],
    "notification_templates": {{}}
  }}
}}

文档内容：
{content[:8000]}"""

    try:
        from src.agent.graph import run_agent_sync
        result = run_agent_sync(prompt, max_turns=3)
        match = re.search(r'\{.*\}', result, re.DOTALL)
        if match:
            return json.loads(match.group())
    except Exception as e:
        logger.error(f"AI解析失败: {e}")

    raise RuntimeError("无法从文档中提取策略配置")
