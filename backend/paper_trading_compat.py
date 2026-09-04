# -*- coding: utf-8 -*-
"""兼容性包装器 - 保持旧代码可以继续工作。

将 paper_trading.xxx 的调用重定向到 paper_trading 包。
"""
# 确保 paper_trading 包可以被导入
try:
    import paper_trading
    # 将包的所有公共接口暴露到模块级别
    for attr in getattr(paper_trading, "__all__", [name for name in dir(paper_trading) if not name.startswith("_")]):
        if hasattr(paper_trading, attr):
            globals()[attr] = getattr(paper_trading, attr)
except ImportError:
    pass
