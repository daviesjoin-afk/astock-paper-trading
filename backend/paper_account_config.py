"""账户配置业务的兼容入口；持久化由 paper_account_ledger 统一负责。"""
from paper_account_ledger import set_cooldown, set_style, write_params

__all__ = ["ACCOUNT_CONFIG_VERSION", "set_cooldown", "set_style", "write_params"]
ACCOUNT_CONFIG_VERSION = "paper-account-config-v1"
