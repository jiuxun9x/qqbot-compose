#!/usr/bin/env python3
"""Run after stopping AstrBot and taking a backup. Keeps routing.json intact."""
from pathlib import Path
import shutil

root=Path(__file__).resolve().parent.parent
source=root/'plugins/astrbot_plugin_qqbot_guard'
target=root/'data/astrbot/plugins/astrbot_plugin_qqbot_guard'
if not (target/'routing.json').exists():raise SystemExit('缺少现有运行配置，请使用 init 初始化新部署。')
for item in source.iterdir():
    if item.suffix=='.py' or item.name=='metadata.yaml':shutil.copyfile(item,target/item.name)
print('插件代码已更新；运行 routing.json 保留。请启动 AstrBot。')
