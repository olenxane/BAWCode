# -*- coding: utf-8 -*-
"""工作区项目标识符：绝对路径计算，事实来源写在项目文件夹 .bawcode/identity.json"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from core.log import get_logger

log = get_logger("project_identity")

IDENTITY_DIRNAME = ".bawcode"
IDENTITY_FILENAME = "identity.json"


def compute_project_id(workspace: Path) -> str:
    resolved = Path(workspace).resolve()
    norm = os.path.normcase(str(resolved))
    digest = hashlib.sha1(norm.encode("utf-8")).hexdigest()[:8]
    return f"{resolved.name}-{digest}"


def identity_path(workspace: Path) -> Path:
    return Path(workspace).resolve() / IDENTITY_DIRNAME / IDENTITY_FILENAME


def ensure_project_identity(workspace: Optional[Path] = None, policy: str = "recompute") -> dict:
    """初始化/读取项目标识符。

    policy:
      - recompute（默认）：文件夹路径与记录不一致时按新路径重算
      - keep：沿用文件内 project_id（即使路径变了）
    """
    ws = Path(workspace or Path.cwd()).resolve()
    path = identity_path(ws)
    computed = compute_project_id(ws)
    now = datetime.now().isoformat(timespec="seconds")

    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            log.warn("identity.json 解析失败，将重建: %s (%s)", path, e)
            data = {}
        stored_id = str(data.get("project_id") or "")
        stored_path = str(data.get("workspace_path") or "")
        same_path = os.path.normcase(stored_path) == os.path.normcase(str(ws)) if stored_path else False

        if stored_id and same_path:
            log.info("项目标识复用: %s", stored_id)
            return {
                "project_id": stored_id,
                "workspace_path": str(ws),
                "workspace_name": ws.name,
                "identity_path": str(path),
                "created_at": data.get("created_at") or now,
                "reused": True,
            }

        if stored_id and policy == "keep":
            log.info("项目标识 keep 策略: %s（路径已变为 %s）", stored_id, ws)
            data.update({"workspace_path": str(ws), "workspace_name": ws.name, "updated_at": now})
            _write_identity(path, data)
            return {
                "project_id": stored_id,
                "workspace_path": str(ws),
                "workspace_name": ws.name,
                "identity_path": str(path),
                "created_at": data.get("created_at") or now,
                "reused": True,
            }

        if stored_id and stored_id != computed:
            log.warn("项目路径变化，标识符重算: %s -> %s", stored_id, computed)

        payload = {
            "version": 1,
            "project_id": stored_id if (stored_id and same_path) else computed,
            "workspace_path": str(ws),
            "workspace_name": ws.name,
            "created_at": data.get("created_at") or now,
            "updated_at": now,
        }
        _write_identity(path, payload)
        log.info("项目标识已写入: %s -> %s", path, payload["project_id"])
        return {
            "project_id": payload["project_id"],
            "workspace_path": str(ws),
            "workspace_name": ws.name,
            "identity_path": str(path),
            "created_at": payload["created_at"],
            "reused": False,
        }

    payload = {
        "version": 1,
        "project_id": computed,
        "workspace_path": str(ws),
        "workspace_name": ws.name,
        "created_at": now,
        "updated_at": now,
    }
    _write_identity(path, payload)
    log.info("项目标识新建: %s", computed)
    return {
        "project_id": computed,
        "workspace_path": str(ws),
        "workspace_name": ws.name,
        "identity_path": str(path),
        "created_at": now,
        "reused": False,
    }


def _write_identity(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
