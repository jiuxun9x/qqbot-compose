# 运维、安全与故障排查

## 2GB 内存

先看 `free -h`、`swapon --show`、`docker stats --no-stream`。没有足够内存时先减少并发和上下文；不要靠反复重启掩盖 OOM。2GB 不保证在所有模型、文件与排版负载下稳定，持续使用 Swap 或响应明显变慢时建议升至 4GB。

缺少 Swap 时可在确认磁盘空间和文件系统支持后配置约 2GB。以下只适用于普通 ext4 等支持常规 swapfile 的系统，已有 `/swapfile` 时停止，Btrfs/ZFS 按系统官方方法设置：

```bash
sudo test ! -e /swapfile
# 上一步必须成功，才继续以下命令；不要覆盖已有文件。
sudo dd if=/dev/zero of=/swapfile bs=1M count=2048 status=progress
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
```

确认生效后再用编辑器审查 `/etc/fstab`，仅添加一次 `/swapfile none swap sw 0 0`。可在独立的 `/etc/sysctl.d/90-qqbot-memory.conf` 中设置 `vm.swappiness=10`，确认不覆盖其他系统管理配置后再加载。不自动更改已有 Swap、SSH 或防火墙。

排版服务已有 768MiB 内存上限、1GiB 内存+Swap 上限、进程数和临时空间限制；这不能保证总内存够用。AstrBot/NapCat 没有设置过小的固定硬上限，需根据实际峰值再决定。排版镜像较大，构建阶段也可能内存不足。检查 OOM：

```bash
sudo docker inspect --format '{{.Name}} restart={{.RestartCount}} oom={{.State.OOMKilled}}' $(sudo docker compose ps -q)
```

## 端口和安全组

| 端口 | 访问范围 |
| --- | --- |
| 实际 SSH 端口，通常 22/TCP | 入站只允许自己的管理地址 |
| 6185/TCP AstrBot | 仅宿主机 127.0.0.1，SSH 隧道 |
| 6099/TCP NapCat | 仅宿主机 127.0.0.1，SSH 隧道 |
| 6199/TCP OneBot | 仅 Docker 内部网络，不发布 |
| 8080/TCP 图片排版 | 仅内部隔离网络，无公网出口 |

无须为这套机器人新增 80/443、FTP、远程桌面或公网 WebSocket 入站。已有网站等服务的规则按它们的需求保留。出方向需要允许 QQ、DNS、时间同步和所用 HTTPS API/软件源。更改云安全组前确认当前 SSH 来源与端口；此仓库不会修改云安全组或系统防火墙。

日志启用 Docker `json-file` 轮转，每服务 10MiB × 3。应用不记录密钥和请求正文；上游服务和错误栈仍可能含消息、账号或登录二维码，只在自己终端查看，分享前脱敏。无须把控制面板暴露公网。

容器不挂 Docker socket、SSH 私钥或宿主机根目录。图片排版只读、无出口，禁用 TeX shell escape，并对数学命令和资源进行限制；这不代表可以无限处理任意恶意文件。仅给可信人员管理权限。

## 备份与恢复

`scripts/backup.sh` 短暂停止本项目服务、生成私有备份，然后只恢复之前在运行的服务。备份包含 QQ 登录状态、会话数据和 `.env`，权限 0600；请加密保存到你控制的位置。不要上传到 GitHub。

恢复建议先在**新的空目录/新服务器**检验，避免覆盖线上数据：

1. 下载与备份匹配的 Git 提交，安装相同 Docker/Compose 环境。
2. 检查备份文件清单 `tar -tzf 备份.tar.gz`，确认来自自己并无异常路径。
3. 在空的部署目录解压；确认 `.env`、`data/` 权限和 NapCat UID/GID 1000。
4. `docker compose build && docker compose up -d`。
5. 检查 WebUI、QQ 登录、Provider、人设、群模式、附件和上下文是否恢复。QQ 登录可能要求重新扫码。

备份含生成配置，但不包含大镜像本身。镜像来源和 Python/系统软件源今后可能变化，长期归档时可另行 `docker image save`，并记录镜像摘要和 Git 提交。

## 更新

不要直接改成全部 `latest`。先备份并记录当前 Git 提交和 `images.lock.json`，阅读上游更新说明；在测试环境验证新镜像、配置迁移及插件 API。

源码更新可 `git pull --ff-only`。现有 `data/astrbot/plugins/` 不会被新源码自动覆盖，以免丢失运行调参。需要升级插件时：

```bash
sudo docker compose stop astrbot
sudo cp -a data/astrbot/plugins/astrbot_plugin_qqbot_guard backups/plugin-before-update
# 保留运行 routing.json；只同步插件 .py 和 metadata.yaml。
sudo python3 scripts/update-plugin.py
sudo docker compose build astrbot renderer
sudo docker compose up -d
```

备份目录已存在时先改成新的名字，避免覆盖。`update-plugin.py` 只更新本项目插件的代码，保留运行路由。插件新增配置字段时需阅读对应版本说明并合并。

镜像升级需同步核对 `.env` 的镜像引用与 `images.lock.json`。只有显式改摘要才会升级锁定的上游镜像；`build --pull` 不会自动把固定摘要变成最新版。更新后检查容器重启次数、OOM、实际收发消息和公式图片。

## 常见故障

- **网页打不开**：先检查 SSH 隧道是否仍运行、服务是否健康、本机端口是否冲突；使用 `localhost`，不要直接访问服务器公网端口。
- **QQ 已登录但无回复**：检查 NapCat 反向 WebSocket 的地址、Token 与 AstrBot 是否匹配；再看本群是否关闭主动回复、是否真正 @、是否排队或 `/stop` 后残留任务。
- **API 探测失败**：核对 Base URL 是否已含 `/v1`、密钥权限、实际模型 ID、余额和出站网络。模型列表成功不代表聊天、视觉或生图也成功。
- **生图只说在画却没图**：检查图片接口的真实返回是否有 `b64_json`；插件使用实际任务状态，不让文本模型虚构已经发图。`/状态` 可手动查询。
- **发文件后说没收到**：查看文件模式、该文件是否属于本群/私聊、下载地址是否有效、文件是否超限。大扫描件请按页拆分，不会默认全文 OCR。
- **LaTeX 失败**：普通文本不应直接泄露模板标签和完整 TeX。用较短问题重试；超出页数/临时空间时分题处理。日志仅供自己查看。
- **模型输出太长**：先改人设和输出上限，避免给所有闲聊开启最高推理预算。API 推理时间与最终文字长度是不同的限制。

## 部署后验收清单

- [ ] 三个容器运行；AstrBot/renderer 健康，无连续重启或 OOM。
- [ ] NapCat 已手动扫码，OneBot 连接建立。
- [ ] 私聊实际回答；先记暗号，再追问，能正确记忆。
- [ ] 换 QQ 用户测试不会继承对方的私人上下文。
- [ ] 群内 @、概率回复、5 秒冷却和 `/主动回复` 开关符合预期。
- [ ] 真实图片/GIF、引用对象、文件后续提问与可选 API 分别测试。
- [ ] 重启后登录状态、人设、对话与群模式仍有效。
- [ ] 持续观察内存与 Swap，再决定是否增加全局并发。
