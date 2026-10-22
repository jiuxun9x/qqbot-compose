# 第三方组件

根目录 MIT 许可仅适用于本仓库自行编写的部署脚本、插件及文档；不会重新许可上游项目。

- AstrBot：官方镜像及依赖，遵守其 [源码和许可](https://github.com/AstrBotDevs/AstrBot)。本仓库在官方镜像上增加文件解析和音频解码依赖。
- NapCat：使用 [NapCat Docker 官方镜像](https://github.com/NapNeko/NapCat-Docker)，代码及 QQ 客户端等组成部分分别受上游条款约束；不在此仓库分发登录数据。
- `renderer/assets/elegantbook.cls`：ElegantLaTeX 的 ElegantBook，保留文件原始头部、作者和 LPPL 1.3c 或更新版本许可。来源：[ElegantBook](https://github.com/ElegantLaTeX/ElegantBook)。
- `renderer/assets/newtx.zip`、`adforn.zip`、`bbding.zip`、`fixtounicode.zip`：完整 CTAN 原始发行包，保留包内来源文件、说明和许可。来源：[newtx](https://ctan.org/pkg/newtx)、[adforn](https://ctan.org/pkg/adforn)、[bbding](https://ctan.org/pkg/bbding)、[fixtounicode](https://ctan.org/pkg/fixtounicode)。各包的许可可能不同，以发行包内文件为准。
- 构建脚本核对上述归档的 SHA-256；在生成镜像的 `/usr/local/share/texmf/doc/fonts/<包名>/` 内保留完整原始包和可找到的许可文件。
- Debian、TeX Live、Fandol、Pandoc、Poppler、Pillow 和其他系统/Python 依赖仍适用各自许可。系统包由官方 Debian 软件源安装；字体使用 TeX Live 自带 Fandol 及上述 CTAN 包。
