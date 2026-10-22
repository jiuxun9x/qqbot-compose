"""Plain-text QQ command help; no API calls or private deployment values."""


def build_help(routing, *, is_admin=False, extra_admin_commands=()):
    group=routing.get('group_reply',{})
    probability=float(group.get('probability',0.05))*100
    cooldown=float(group.get('cooldown_seconds',5))
    image='已启用' if routing.get('image_generation',{}).get('enabled') else '未启用，需先配置图片 API'
    search='已启用' if routing.get('web_search',{}).get('enabled') else '未启用，需先配置搜索 API'
    voice='已启用' if routing.get('voice_input',{}).get('enabled') else '未启用，需先配置转写 API'
    lines=[
        '常用指令（/help 或 /帮助）',
        '/new：新建当前会话。',
        '/reset：清空当前会话上下文。',
        '/stop：停止自己的任务及待处理消息。',
        '/状态（/status）：手动查看任务进度和排队。',
        '/stats：查看当前会话的用量统计。',
        '',
        '图片、文件与搜索',
        '/画图 描述（/生图、/draw）：生成图片。'+image+'。',
        '生成后可说“重新画”“换成水彩风格”“重发图片”；重发复用已缓存原图。',
        '发图片/GIF 后直接问，或引用具体图片；发文件后再提问即可。',
        '/文件自动回复 状态：查看收到文件后是否自动回答（也可用 /文件回复）。',
        '/搜索 问题（/联网、/search）：联网查询。'+search+'。',
        '/来源（/sources）：追问最近检索的实际来源。默认回答不附链接。',
        '语音转文字：'+voice+'。',
        '说“深度思考＋问题”可要求认真分析；数学用公式图片、代码用排版图片，日常聊天用纯文本。',
        '',
        '群聊',
        '/主动回复 状态：查看本群模式。',
        '开启时先判断说话对象；普通插话概率 '+format(probability,'g')+'%，冷却 '+format(cooldown,'g')+' 秒。',
        '关闭时需真正 @我 才回答；每个群独立保存。',
    ]
    if is_admin:
        lines.extend([
            '', '管理员指令',
            '/主动回复 开｜关：在目标群切换自动判断回复 / 仅 @ 回复。',
            '/文件自动回复 开｜关：切换发文件后是否立即分析；群内切换需管理员，私聊可管理自己的设置。',
            '/搜索池：查看搜索凭据的本地配额与冷却，不显示密钥。',
            '/provider：查看或切换框架模型服务；自动路由仍按已配置角色选择。',
            '管理员在当前没有自己的任务时使用 /stop，会停止全部任务。',
        ])
        lines.extend(str(line) for line in extra_admin_commands)
    else:
        lines.extend(['私聊可用 /文件自动回复 开｜关 管理自己的文件模式；群内切换和主动回复开关需机器人管理员。'])
    return '\n'.join(lines)
