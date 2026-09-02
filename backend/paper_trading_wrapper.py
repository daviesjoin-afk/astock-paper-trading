# -*- coding: utf-8 -*-
"""paper_trading.py - 向后兼容包装器。

这个文件保持向后兼容，所有函数仍然可以通过 import paper_trading 访问。
实际代码已迁移到 paper_trading/ 包中的各模块。
"""
from paper_trading import *

# 确保所有公共接口都可用
from paper_trading import __all__
