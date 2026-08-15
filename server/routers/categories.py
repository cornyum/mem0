"""自定义分类管理路由（Custom memory category taxonomy）.

管理员通过 PUT /categories 定义一套记忆分类体系（名称+描述，≤50 个）。
定义被编译为抽取指令注入 Memory 配置（见 server_state.apply_category_instructions），
SDK 在记忆写入时按体系自动打分类标签，标签落入记忆 payload 的 categories 字段。

存储复用 Settings 表（key=memory_categories，无新表无迁移）。
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from auth import require_admin, verify_auth
from db import get_db
from models import Settings, User

router = APIRouter(prefix="/categories", tags=["categories"])

CATEGORIES_SETTINGS_KEY = "memory_categories"
MAX_CATEGORIES = 50
MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 200


class CategoryItem(BaseModel):
    name: str
    description: str


class CategoriesResponse(BaseModel):
    categories: list[CategoryItem]
    updated_at: str | None


class UpdateCategoriesRequest(BaseModel):
    # Kept loose so contract violations surface as 400 (PRD 3.1) instead of Pydantic's 422.
    categories: Any


class CategoriesUpdateResponse(BaseModel):
    message: str
    count: int


def _validate_categories(raw) -> list[dict[str, str]]:
    """Validate and normalize an incoming taxonomy. Raises HTTPException 400 on contract violations."""
    if not isinstance(raw, list):
        raise HTTPException(status_code=400, detail="'categories' must be a list.")
    if len(raw) > MAX_CATEGORIES:
        raise HTTPException(status_code=400, detail=f"At most {MAX_CATEGORIES} categories are allowed.")

    cleaned: list[dict[str, str]] = []
    seen_names: set[str] = set()
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            raise HTTPException(status_code=400, detail=f"categories[{idx}] must be an object with 'name' and 'description'.")
        name = item.get("name")
        description = item.get("description")
        if description is None:
            description = ""
        if not isinstance(name, str) or not name.strip():
            raise HTTPException(status_code=400, detail=f"categories[{idx}].name is required and must be a non-empty string.")
        name = name.strip()
        if len(name) > MAX_NAME_LENGTH:
            raise HTTPException(status_code=400, detail=f"categories[{idx}].name must be at most {MAX_NAME_LENGTH} characters.")
        if not isinstance(description, str):
            raise HTTPException(status_code=400, detail=f"categories[{idx}].description must be a string.")
        if len(description) > MAX_DESCRIPTION_LENGTH:
            raise HTTPException(
                status_code=400, detail=f"categories[{idx}].description must be at most {MAX_DESCRIPTION_LENGTH} characters."
            )
        if name in seen_names:
            raise HTTPException(status_code=400, detail=f"Duplicate category name '{name}' after trimming.")
        seen_names.add(name)
        cleaned.append({"name": name, "description": description})
    return cleaned


def _save_categories_setting(db: Session, serialized: str) -> None:
    """Dialect-aware upsert, mirroring server_state._save_overrides."""
    dialect_name = db.bind.dialect.name if db.bind is not None else ""
    if dialect_name == "mysql":
        from sqlalchemy.dialects.mysql import insert

        stmt = (
            insert(Settings)
            .values(key=CATEGORIES_SETTINGS_KEY, value=serialized)
            .on_duplicate_key_update(value=serialized)
        )
    else:
        from sqlalchemy.dialects.postgresql import insert

        stmt = (
            insert(Settings)
            .values(key=CATEGORIES_SETTINGS_KEY, value=serialized)
            .on_conflict_do_update(
                index_elements=[Settings.key],
                set_={"value": serialized},
            )
        )
    db.execute(stmt)
    db.commit()


@router.get("", response_model=CategoriesResponse)
def get_categories(_user: User | None = Depends(verify_auth), db: Session = Depends(get_db)):
    """Read the deployment's category taxonomy. Undefined or corrupt storage yields the empty state."""
    categories: list[CategoryItem] = []
    updated_at = None
    row = db.get(Settings, CATEGORIES_SETTINGS_KEY)
    if row is not None and row.value:
        try:
            data = json.loads(row.value)
        except (json.JSONDecodeError, TypeError, ValueError):
            data = None
        if isinstance(data, dict):
            updated_at = data.get("updated_at")
            raw_categories = data.get("categories")
            if isinstance(raw_categories, list):
                for item in raw_categories:
                    if isinstance(item, dict) and str(item.get("name") or "").strip():
                        categories.append(
                            CategoryItem(name=str(item["name"]).strip(), description=str(item.get("description") or ""))
                        )
    return CategoriesResponse(categories=categories, updated_at=updated_at)


@router.put("", response_model=CategoriesUpdateResponse)
def update_categories(body: UpdateCategoriesRequest, _admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Replace the full category taxonomy. Requires admin role."""
    cleaned = _validate_categories(body.categories)
    updated_at = datetime.now(timezone.utc).isoformat()
    serialized = json.dumps({"categories": cleaned, "updated_at": updated_at}, ensure_ascii=False)
    _save_categories_setting(db, serialized)
    # Rebuild the Memory instance so the new taxonomy is compiled into the
    # extraction instructions immediately (no restart needed). Empty update:
    # config merge is a no-op, from_config re-applies category instructions.
    message = "分类已更新"
    try:
        from server_state import update_config

        update_config({})
    except Exception:
        logging.exception("Failed to refresh memory config after category update")
        message = "分类已保存，但运行配置刷新失败，重启服务后生效"
    return CategoriesUpdateResponse(message=message, count=len(cleaned))
