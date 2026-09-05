#!/usr/bin/env python3
"""Setup script for AIOS — pip install 兼容"""
from setuptools import setup

setup(
    name="aios",
    version="5.2.9",
    description="AIOS — AI Agent Operating System",
    python_requires=">=3.11",
    install_requires=[
        "redis>=5.0",
        "psutil>=5.9",
        "requests>=2.31",
        "aiohttp>=3.9",
        "httpx>=0.27",
    ],
    extras_require={
        "dev": ["pytest>=8.0", "pytest-asyncio>=0.23"],
    },
    include_package_data=True,
)

# Usage:
#   pip install -e ${AIOS_HOME}    # 开发模式安装
#   pip install ${AIOS_HOME}        # 正式安装
