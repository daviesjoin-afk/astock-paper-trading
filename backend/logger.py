# -*- coding: utf-8 -*-
"""统一日志模块：替换所有裸 except:pass，提供结构化日志"""
import logging
import os

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reports")
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "system.log"), encoding="utf-8"),
        logging.StreamHandler(),
    ],
)


def get_logger(name):
    return logging.getLogger(name)
