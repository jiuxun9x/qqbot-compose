#!/usr/bin/env python3
"""Read-only Linux and Docker preflight; no credentials are printed."""
import os
from pathlib import Path
import platform
import shutil
import socket
import subprocess


def command(args):
    if not shutil.which(args[0]):print(args[0]+': 未安装');return
    result=subprocess.run(args,text=True,capture_output=True)
    print((result.stdout or result.stderr).strip())


def main():
    print(Path('/etc/os-release').read_text() if Path('/etc/os-release').exists() else platform.platform())
    print('逻辑 CPU:',os.cpu_count())
    command(['free','-h'])
    usage=shutil.disk_usage(Path(__file__).resolve().parent.parent)
    print('目录所在磁盘可用 GiB:',round(usage.free/1024**3,2))
    command(['docker','--version']);command(['docker','compose','version'])
    command(['docker','ps','--format','table {{.Names}}\t{{.Status}}\t{{.Ports}}'])
    for port in (6185,6099):
        with socket.socket() as sock:
            try:sock.bind(('127.0.0.1',port));print('本机端口',port,'空闲')
            except OSError:print('本机端口',port,'已占用，须调整 Compose 宿主机端口')
    print('只读检查完成；未改动系统、已有服务或网络规则。')


if __name__=='__main__':main()
