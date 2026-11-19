"""测试包的初始化：把插件目录接入 ``sys.path``。

测试文件在模块顶层直接 ``import neko_lol_companion`` / ``import fixtures``，这要求
搜索路径**在导入测试模块之前**就配好。放在包初始化里正好满足：无论用
``python -m unittest tests.test_offline`` 还是 pytest，导入
``tests.test_offline`` 都会先执行本文件。

## 两种目录布局都要支持

* **开发目录**：``Orca工作区/neko_lol_companion/`` —— 父目录加进 ``sys.path`` 后
  ``import neko_lol_companion`` 成立。
* **发布仓库**（插件市场要求根目录直接是 ``plugin.toml``）：目录本身没有
  包名，只能把插件目录注册成名为 ``neko_lol_companion`` 的包再导入。

放在这里而不是每个测试文件里，是为了让测试文件的 import 全部待在文件
顶部 —— 否则会被 lint 判为 ``E402``（module level import not at top of file）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

#: 插件目录本身（仓库根 / 开发目录）
PLUGIN_DIR: Path = Path(__file__).resolve().parent.parent
#: 测试与 fixtures 所在目录
TESTS_DIR: Path = Path(__file__).resolve().parent


def _prepare_paths() -> None:
    """把插件目录和 tests 目录加进 sys.path。"""
    for path in (str(TESTS_DIR), str(PLUGIN_DIR.parent)):
        if path not in sys.path:
            sys.path.insert(0, path)


def _register_as_package() -> None:
    """把插件目录按 ``neko_lol_companion`` 这个名字注册成包（发布仓库布局）。"""
    if PLUGIN_DIR.name == "neko_lol_companion" or "neko_lol_companion" in sys.modules:
        return
    init = PLUGIN_DIR / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        "neko_lol_companion", init, submodule_search_locations=[str(PLUGIN_DIR)]
    )
    if spec is None or spec.loader is None:  # pragma: no cover - 文件缺失才走到
        raise ImportError(f"无法从 {init} 加载插件包")
    module = importlib.util.module_from_spec(spec)
    sys.modules["neko_lol_companion"] = module
    spec.loader.exec_module(module)


_prepare_paths()
_register_as_package()