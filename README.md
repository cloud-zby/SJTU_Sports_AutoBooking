# SJTU-交我办运动场馆预约
配置环境
```bash
# 使用虚拟环境
sudo apt update
sudo apt install python3-full python3-venv
python3 -m venv .venv
# 安装OCR组件
sudo apt install tesseract-ocr  # In windows, `choco install tesseract`
# 安装依赖
source .venv/bin/activate
pip install selenium pillow pytesseract opencv-python numpy
```
自动预约（自动尝试至多5次OCR识别与AJ-Captcha-blockPuzzle与AJ-Captcha-clickWord）
```bash
# 立刻预约（需要替换为自己的JAccount账户名和密码）
python main.py --user '...' --password '...' --date 2026-11-30 --venue '学生中心' --item-name '交谊厅' --period 14 --test

# 正式模式：默认在每日 12:00-12:10 窗口内反复尝试
python main.py --user '...' --password '...' --date 2026-11-26 --venue '学生中心' --item-name '健身房' --period 9,13,14

# 监控模式：拉起后每隔1分钟一直反复监测，直到监测目标可预约就发送邮件通知
python main.py --user '...' --password '...' --date 2026-11-30 --venue '学生中心' --item-name '交谊厅' --period 14,15 --only-moniter --monitor-interval 60 --monitor-repeat --smtp-user ... --smtp-password ... --smtp-host ... --smtp-port ... --mail-to ...
```
参考仓库：https://github.com/Chen-WH/SJTU-Sports.git
