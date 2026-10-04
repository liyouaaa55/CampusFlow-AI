@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ============================================
echo   CampusFlow AI  -  本地一键启动
echo ============================================
echo.

rem ---------- 1. 检查项目虚拟环境 ----------
if not exist ".venv\Scripts\python.exe" (
    echo [错误] 未找到项目虚拟环境 .venv
    echo.
    echo 请先在本目录执行：
    echo     python -m venv .venv
    echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

rem ---------- 2. 检查环境变量文件 ----------
if not exist ".env" (
    echo [提示] 未找到 .env 文件，事项识别功能将不可用。
    echo        请复制 .env.example 为 .env 并填写 API Key。
    echo.
)

rem ---------- 3. 启动（固定使用项目虚拟环境，避免误用 Anaconda）----------
echo [信息] 解释器：.venv\Scripts\python.exe
echo [信息] 地址　：http://localhost:8501
echo [信息] 停止　：按 Ctrl+C
echo.

".venv\Scripts\python.exe" -m streamlit run app.py %*
set "EXIT_CODE=%ERRORLEVEL%"

echo.
if not "%EXIT_CODE%"=="0" (
    echo [错误] 服务异常退出，退出码 %EXIT_CODE%
    echo        若提示缺少依赖，请执行：
    echo        .venv\Scripts\python.exe -m pip install -r requirements.txt
) else (
    echo [信息] 服务已停止。
)
echo.
pause
exit /b %EXIT_CODE%
