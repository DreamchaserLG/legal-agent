from __future__ import annotations

"""
数据源配置服务 - 支持多种数据源的统一管理
"""

import os
from typing import Dict, List, Optional

from app.core.config import settings


# 数据源类型定义
DATA_SOURCE_TYPES = {
    "demo": {
        "label": "演示数据",
        "label_en": "Demo Data",
        "description": "使用本地演示数据，适合测试和开发",
        "requires_api_key": False,
        "requires_network": False,
    },
    "canlii_api": {
        "label": "CanLII API",
        "label_en": "CanLII API",
        "description": "使用CanLII官方API获取案例数据",
        "requires_api_key": True,
        "requires_network": True,
    },
    "canlii_rss": {
        "label": "CanLII RSS",
        "label_en": "CanLII RSS",
        "description": "通过CanLII RSS订阅获取案例数据",
        "requires_api_key": False,
        "requires_network": True,
    },
    "open_canada": {
        "label": "Open Canada",
        "label_en": "Open Canada",
        "description": "使用加拿大政府开放数据API",
        "requires_api_key": False,
        "requires_network": True,
    },
    "justice_laws": {
        "label": "Justice Laws",
        "label_en": "Justice Laws",
        "description": "从Justice Laws网站获取联邦法规",
        "requires_api_key": False,
        "requires_network": True,
    },
    "ontario_elaws": {
        "label": "Ontario e-Laws",
        "label_en": "Ontario e-Laws",
        "description": "从Ontario e-Laws获取安大略省法规",
        "requires_api_key": False,
        "requires_network": True,
    },
    "custom": {
        "label": "自定义数据源",
        "label_en": "Custom Source",
        "description": "使用自定义URL或API获取数据",
        "requires_api_key": False,
        "requires_network": True,
    },
}


def get_data_source_config(source_type: str) -> Dict:
    """获取数据源配置"""
    return DATA_SOURCE_TYPES.get(source_type, DATA_SOURCE_TYPES["demo"])


def get_current_legislation_source() -> str:
    """获取当前法规数据源"""
    return settings.canada_legislation_source


def get_current_case_source() -> str:
    """获取当前案例数据源"""
    return settings.canada_case_source


def is_api_key_configured(source_type: str) -> bool:
    """检查API密钥是否已配置"""
    if source_type == "canlii_api":
        return bool(settings.canlii_api_key)
    return True


def get_available_sources() -> List[Dict]:
    """获取所有可用的数据源"""
    sources = []
    for source_type, config in DATA_SOURCE_TYPES.items():
        source_info = {
            "type": source_type,
            "label": config["label"],
            "label_en": config["label_en"],
            "description": config["description"],
            "requires_api_key": config["requires_api_key"],
            "requires_network": config["requires_network"],
            "is_configured": True,
            "is_current_legislation": source_type == settings.canada_legislation_source,
            "is_current_case": source_type == settings.canada_case_source,
        }

        # 检查是否需要API密钥
        if config["requires_api_key"]:
            source_info["is_configured"] = is_api_key_configured(source_type)

        sources.append(source_info)

    return sources


def update_legislation_source(source_type: str) -> bool:
    """更新法规数据源"""
    if source_type not in DATA_SOURCE_TYPES:
        return False

    config = DATA_SOURCE_TYPES[source_type]
    if config["requires_api_key"] and not is_api_key_configured(source_type):
        return False

    # 更新环境变量
    os.environ["CANADA_LEGISLATION_SOURCE"] = source_type
    settings.canada_legislation_source = source_type
    return True


def update_case_source(source_type: str) -> bool:
    """更新案例数据源"""
    if source_type not in DATA_SOURCE_TYPES:
        return False

    config = DATA_SOURCE_TYPES[source_type]
    if config["requires_api_key"] and not is_api_key_configured(source_type):
        return False

    # 更新环境变量
    os.environ["CANADA_CASE_SOURCE"] = source_type
    settings.canada_case_source = source_type
    return True


def get_source_status() -> Dict:
    """获取所有数据源的状态"""
    return {
        "legislation_source": get_current_legislation_source(),
        "case_source": get_current_case_source(),
        "legislation_config": get_data_source_config(get_current_legislation_source()),
        "case_config": get_data_source_config(get_current_case_source()),
        "available_sources": get_available_sources(),
    }
