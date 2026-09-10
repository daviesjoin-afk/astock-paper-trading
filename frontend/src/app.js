/* PR-55：前端入口。
   装载顺序 = 语义：feature 模块（纯声明）→ 兼容桥 → boot（顶层语句，按原顺序）。 */
window.__ASTOCK_ADAPTIVE_UI_BUILD__='20260910-strategy-console-v1';

import "./bridge.js";
import "./boot.js";
