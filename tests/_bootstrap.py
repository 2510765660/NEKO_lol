"""让测试在两种目录布局下都能 import 到插件本体。

两种布局都真实存在，必须同时支持：

* **开发目录**：``Orca工作区/neko_lol/`` —— 包名就是目录名，父目录加进
  ``sys.path`` 后 ``import neko_lol`` 成立。
* **发布仓库**（插件市场要求根目录直接是 ``plugin.toml``）：
  目录本身没有包名，只能把插件目录本身加进 ``sys.path``，
  然后 ``import __init__`` 之类按顶层模块导入。

实现上：优先尝试 ``import neko_lol``（开发目录），失败就把当前插件目录
以 ``neko_lol`` 这个名字注册成包（发布仓库）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent


def ensure_plugin_package() -> str:
    """确保能 import 到插件包，返回可用的模块名。"""
    # 1) 开发目录：上一级就是父目录，neko_lol 是普通包
    if PLUGIN_DIR.name == "neko_lol" and str(PLUGIN_DIR.parent) not in sys.path:
        sys.path.insert(0, str(PLUGIN_DIR.parent))
    if PLUGIN_DIR.name == "neko_lol":
        try:
            import neko_lol  # noqa: F401
            return "neko_lol"
        except ImportError:
            pass

    # 2) 发布仓库：目录名不是 neko_lol，手动按包加载
    if "neko_lol" in sys.modules:
        return "neko_lol"
    init = PLUGIN_DIR / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        "neko_lol",
        init,
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    if spec is None or spec.loader is None:      # pragma: no cover
        raise ImportError(f"无法从 {init} 加载插件包")
    module = importlib.util.module_from_spec(spec)
    sys.modules["neko_lol"] = module
    spec.loader.exec_module(module)
    return "neko_lol"
