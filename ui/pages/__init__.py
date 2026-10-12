"""页面包：五个业务页面，每个模块对外暴露 render() 函数。

1. dashboard_page.py   智能驾驶舱
2. upload_page.py      会议材料与任务生成（上传 + 解析 + 识别 + 确认入库）
3. tasks_page.py       任务督办中心（下发前分析 → 确认 → 通知预览 → 模拟下发）
4. inspection_page.py  执行反馈与检查（执行反馈 → 持续督办 → 核验 → 正式闭环）
5. report_page.py      工作检查报告（正式文字总结 + 完整明细表 + 导出）

nav.py                 页面名称与顺序的唯一来源
execution_panel.py     执行期督办面板（第三阶段从 tasks_page 抽出，由第四页调用）
submission_view.py     下发面板 / 工作检查窗口（被 tasks_page 复用）
review_page.py         识别结果编辑与确认入库（被 upload_page 内联复用）
"""
