"""结构化台账解析：把表格型材料逐行确定性地映射为任务。

为什么需要这个模块
------------------
稳定性评测（docs/dev/stability_report.md）确认：Excel 台账类材料走大模型抽取时，
同一份表格连续 5 次调用会得到不同结果——材料7 出现 15/18/18/18/18（3 条 completed 行
整行丢失），材料13 的 1 条 completed 行 5 次全部丢失，且 owner/department/deadline
在不同 run 间跳变。

根因是：大模型被要求对「表格里已经写明白的行」再做一次判断——判断这行算不算任务、
算完成还是待办、负责人是谁。表格本身已给出确定的行边界与字段值，这一步判断既无必要，
也不可复现。

本模块的定位
------------
对具备明确表头结构的表格材料，跳过大模型，逐行直接映射：

    原始单元格  -->  任务字段

三条硬约束
----------
1. 一行原始数据最多对应一个任务：不合并相邻行、不把一行拆成多条；
2. 原始数据优先：字段值一律取自单元格，为空即 None，绝不猜测；
3. 识别不到就退回原流程：返回 None 表示「不像台账」，由调用方继续走大模型路径。

priority / risk_level / need_confirm 不在此处推断，继续交给 PlannerAgent。
"""

from __future__ import annotations

from typing import Any

# ============================================================
# 一、表头列名同义词
# ============================================================

COLUMN_SYNONYMS: dict[str, tuple[str, ...]] = {
    "title": (
        "事项", "任务", "工作内容", "工作事项", "活动事项", "隐患问题", "隐患",
        "问题", "名称", "内容", "事项名称", "任务名称", "任务名",
    ),
    "department": (
        "部门", "责任部门", "责任单位", "负责部门", "牵头部门", "承办部门",
        "责任部门名称", "单位",
    ),
    "owner": (
        "负责人", "责任人", "联系人", "责任人员", "负责人姓名", "分管领导",
    ),
    "deadline": (
        "截止时间", "完成时间", "时限", "完成期限", "整改期限", "期限",
        "截止日期", "完成日期", "办结时限", "时间要求",
    ),
    "status": (
        "状态", "进度", "当前状态", "任务状态", "完成情况", "进展", "执行状态",
    ),
    "description": (
        "备注", "说明", "工作要求", "备注说明", "情况说明", "内容要求",
    ),
    # 序号列不映射到任何任务字段，仅用于「这一行是不是数据行」的判定，
    # 以便把表格尾部的汇总块（任务总数 | 18 / 已完成 | 3）排除在外。
    "seq": ("序号", "编号", "序 号", "项次", "序"),
}

MIN_RECOGNIZED_COLUMNS: int = 3
MIN_DATA_ROWS: int = 2

# 反向索引：表头文字 -> 规范字段（最长匹配优先，避免「工作内容」被「内容」抢先）
_HEADER_INDEX: list[tuple[str, str]] = sorted(
    ((kw, field) for field, kws in COLUMN_SYNONYMS.items() for kw in kws),
    key=lambda kv: len(kv[0]),
    reverse=True,
)

# ============================================================
# 二、状态映射
# ============================================================

# 只认明确写法，不做子串猜测（否则「未完成」会被「完成」误判为 completed）。
STATUS_TEXT_MAP: dict[str, str] = {
    "已完成": "completed", "完成": "completed", "已结束": "completed",
    "已落实": "completed", "已办结": "completed",
    "进行中": "processing", "正在进行": "processing", "推进中": "processing",
    "正在推进": "processing", "开展中": "processing",
    # 隐患整改类台账的常用写法，语义等同「进行中」
    # （例：安全隐患整改台账.xlsx 的「当前状态」列）
    "整改中": "processing",
    "未开始": "pending", "待办": "pending", "未完成": "pending",
    "未启动": "pending", "待开始": "pending", "待启动": "pending",
    "已取消": "cancelled", "取消": "cancelled",
    "已终止": "cancelled", "终止": "cancelled", "作废": "cancelled",
}


def map_status(raw: Any) -> str | None:
    """表格状态文字 -> 四态之一。

    未命中时返回 None，由调用方交给既有 normalize 规则落回默认状态，
    不在本模块引入第二套状态判断口径。
    """
    if raw is None:
        return None
    text = "".join(str(raw).split())
    if not text:
        return None
    return STATUS_TEXT_MAP.get(text)


# ============================================================
# 三、表格识别
# ============================================================


def split_row(line: str) -> list[str]:
    """按 `|` 拆分单元格并去除首尾空白。"""
    return [cell.strip() for cell in line.split("|")]


def _match_columns(cells: list[str]) -> dict[str, int]:
    """把一行表头映射为 {规范字段: 列下标}。同一字段只取第一次出现的位置。"""
    mapping: dict[str, int] = {}
    for idx, cell in enumerate(cells):
        text = "".join(cell.split())
        if not text:
            continue
        for kw, field in _HEADER_INDEX:
            if text == kw or (len(kw) >= 2 and kw in text):
                mapping.setdefault(field, idx)
                break
    return mapping


def detect_header(lines: list[str]) -> tuple[int, dict[str, int], int] | None:
    """在文本行中查找台账表头。

    全部满足才认定：
      1. 该行能映射出 >= MIN_RECOGNIZED_COLUMNS 个规范字段；
      2. 其中必须包含 title（没有事项列就不算任务表）；
      3. 其后至少还有 MIN_DATA_ROWS 行、且每行单元格数与表头一致。

    「单元格数一致」这条同时挡掉了表格尾部的汇总块
    （如「任务总数 | 18 | …」「已完成 | 3」），防止把汇总行当成任务行。

    返回 (表头行号, 列映射, 列数)；未找到返回 None。
    """
    for i, line in enumerate(lines):
        if "|" not in line:
            continue
        cells = split_row(line)
        if len(cells) < MIN_RECOGNIZED_COLUMNS:
            continue

        mapping = _match_columns(cells)
        if "title" not in mapping or len(mapping) < MIN_RECOGNIZED_COLUMNS:
            continue

        width = len(cells)
        rows = 0
        for follow in lines[i + 1:]:
            if "|" not in follow:
                if rows:
                    break
                continue
            if len(split_row(follow)) != width:
                continue
            rows += 1
        if rows >= MIN_DATA_ROWS:
            return i, mapping, width
    return None


# ============================================================
# 四、逐行映射
# ============================================================


def _cell(cells: list[str], index: int | None) -> str | None:
    """取单元格文本；越界或空串一律返回 None（不猜测、不补全）。"""
    if index is None or index >= len(cells):
        return None
    text = cells[index].strip()
    return text or None


def _is_seq(value: str | None) -> bool:
    """序号单元格是否为数字（用于区分数据行与表格尾部的汇总行）。"""
    if value is None:
        return False
    text = "".join(value.split())
    if not text:
        return False
    return text.replace(".", "", 1).replace("-", "", 1).isdigit()


def is_ledger_frame(frame: Any) -> bool:
    """判断 pandas DataFrame 是否为结构化台账（用于解析阶段决定是否保留空单元格）。

    保留空单元格是维持列对齐的前提：Excel 中某格为空时，若直接丢弃，
    后续所有列都会左移一位，把「负责人」错当成「截止时间」。
    """
    try:
        head = frame.head(10)
    except AttributeError:
        return False
    for _, row in head.iterrows():
        cells = ["" if _is_na(v) else str(v).strip() for v in row.tolist()]
        mapping = _match_columns(cells)
        if "title" in mapping and len(mapping) >= MIN_RECOGNIZED_COLUMNS:
            return True
    return False


def _is_na(value: Any) -> bool:
    """判断是否为 pandas 缺失值（不依赖 pandas 本身，便于单测）。"""
    text = str(value).strip().lower()
    return text in ("", "nan", "none", "nat")


def parse_ledger(text: str) -> list[dict[str, Any]] | None:
    """把结构化台账文本逐行映射为任务列表。

    返回 None 表示「不像台账」，调用方应继续走大模型抽取路径。
    识别成功时返回每个数据行一个任务，字段直接取自原始单元格：
    title/department/owner/deadline/status/description。

    priority 固定为 None：按需求不由表格文本猜测，交由 PlannerAgent 推断。
    """
    if not text or "|" not in text:
        return None

    lines = text.split("\n")
    found = detect_header(lines)
    if found is None:
        return None

    header_at, columns, width = found
    has_seq = "seq" in columns
    mapped = ("title", "department", "owner", "deadline", "status", "description")
    tasks: list[dict[str, Any]] = []

    for line in lines[header_at + 1:]:
        if "|" not in line:
            if tasks:
                break
            continue
        cells = split_row(line)
        if len(cells) != width:
            # 列数不一致说明该行不是数据行（如表格尾部的汇总块），跳过
            continue

        title = _cell(cells, columns.get("title"))
        if not title:
            continue

        if has_seq:
            # 有序号列时，只认序号为数字的行，用它把汇总行挡在外面
            if not _is_seq(_cell(cells, columns.get("seq"))):
                continue
        else:
            # 无序号列时要求至少 3 个业务字段有值，避免把汇总块当成数据行
            filled = sum(1 for f in mapped if _cell(cells, columns.get(f)))
            if filled < 3:
                continue

        tasks.append({
            "title": title,
            "department": _cell(cells, columns.get("department")),
            "owner": _cell(cells, columns.get("owner")),
            "deadline": _cell(cells, columns.get("deadline")),
            # priority 不由表格文本猜测，交给 PlannerAgent
            "priority": None,
            "status": map_status(_cell(cells, columns.get("status"))),
            "description": _cell(cells, columns.get("description")),
        })

    return tasks or None
