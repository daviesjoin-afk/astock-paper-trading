# -*- coding: utf-8 -*-
"""paper_trading.py - 向后兼容包装器。

这个文件保持向后兼容，所有函数仍然可以通过 import paper_trading 访问。
实际代码已迁移到 paper_trading/ 包中的各模块。
"""
from paper_trading import *

# 旧部署中的 paper_trading.py 没有定义 __all__。包装模块不能因为
# 导出元数据缺失而让旧调用方导入失败。
import paper_trading as _paper_trading
__all__ = getattr(_paper_trading, "__all__", [name for name in dir(_paper_trading) if not name.startswith("_")])
