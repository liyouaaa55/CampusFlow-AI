"""演示环境启动入口：让**同一份业务代码**使用独立演示数据库。

用法::

    streamlit run docs/dev/demo_app.py --server.port 8502

与开发环境的区别只有一处：``from app import main`` 之前把全部落点切到
``data/demo/``（演示数据库 + 上传材料 + 反馈/回执附件）。业务代码、业务规则、
数据库表结构一行都不改；``streamlit run app.py`` 的行为完全保持原样。

顺序为什么重要
--------------
``config.settings.DATA_DIR / UPLOAD_DIR / DB_PATH`` 是**导入时求值**的模块常量，
而 ``ui.pages.submission_view.SUBMISSION_DIR`` 更是在它自己被导入时
由 ``DATA_DIR`` 推导出来的。因此必须：

    1. 先改 ``settings.DATA_DIR / UPLOAD_DIR / DB_PATH``；
    2. 再导入 ``submission_view``（触发 demo_db.activate_demo_paths 内部导入）；
    3. 最后才 ``from app import main``（它会连带导入全部页面）。

否则附件会写回开发目录 ``data/submissions``。
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE: Path = Path(__file__).resolve().parent
# docs/dev 需要在 sys.path 中（demo_db），项目根也需要（database / config / ui）
for _path in (str(HERE), str(HERE.parent.parent)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from demo_db import DEV_DB_PATH, activate_demo_paths, init_demo_db  # noqa: E402

# 1) 先切路径（含目录创建），再导入任何依赖这些路径的业务模块
PATHS = activate_demo_paths()

# 2) 演示库不存在时按既有建表逻辑初始化为空库（幂等，不导入开发数据）
DEMO_DB = init_demo_db(PATHS)

# 3) 提示信息走 stderr：Streamlit 要求 set_page_config 必须是首个 Streamlit 命令，
#    因此此处不能调用任何 st.* 函数。侧边栏会由 app.py 自动显示 demo.db。
print("=" * 68, file=sys.stderr)
print("[演示环境] CampusFlow AI —— 独立演示数据", file=sys.stderr)
print(f"[演示环境] 演示数据库  : {DEMO_DB}", file=sys.stderr)
print(f"[演示环境] 上传材料目录: {PATHS.uploads}", file=sys.stderr)
print(f"[演示环境] 附件目录    : {PATHS.submissions}", file=sys.stderr)
print(f"[演示环境] 开发库      : {DEV_DB_PATH}（本入口不会读写它）", file=sys.stderr)
print(f"[演示环境] 基线快照    : {PATHS.baseline}", file=sys.stderr)
print("[演示环境] 常用命令    : python docs/dev/demo_db.py status | backup | reset", file=sys.stderr)
print("=" * 68, file=sys.stderr)

# 4) 最后导入并运行真正的应用入口
from app import main  # noqa: E402

main()