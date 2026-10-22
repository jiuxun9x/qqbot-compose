#!/usr/bin/env python3
"""Fresh-deployment configuration; API secrets are read with hidden input."""
import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT=Path(__file__).resolve().parent.parent
os.umask(0o077)

def save(path, value, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, ensure_ascii=False, indent=2) + "\n" if not isinstance(value, str) else value
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".qqbot-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

def env_read():
    values={}
    for line in (ROOT/'.env').read_text().splitlines():
        if not line.strip() or line.lstrip().startswith('#'):continue
        key,value=line.split('=',1)
        if value.startswith("'") and value.endswith("'"):value=value[1:-1].replace("\\'","'")
        values[key]=value
    return values


def env_write(values):
    lines=['# Generated locally; keep private.']
    for key,value in values.items():
        value=str(value)
        if any(c in value for c in '\n\r\x00'):raise ValueError('配置值不得包含换行或空字符')
        lines.append(key+"='"+value.replace("'","\\'")+"'")
    save(ROOT/'.env','\n'.join(lines)+'\n')


def validate_base(base):
    url=urllib.parse.urlsplit(base)
    if url.scheme!='https' or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError('请使用不含账号、查询参数的 HTTPS API Base URL')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


def remote(base,key,path,body=None,timeout=120):
    data=None if body is None else json.dumps(body).encode()
    request=urllib.request.Request(base.rstrip('/')+path,data=data,headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'})
    try:
        with urllib.request.build_opener(NoRedirect).open(request,timeout=timeout) as response:raw=response.read(2*1024**2+1)
    except urllib.error.HTTPError as error:raise RuntimeError('API HTTP '+str(error.code)+'；未输出响应正文') from None
    except (OSError,urllib.error.URLError):raise RuntimeError('API 网络连接失败；请检查地址、DNS 和出站网络') from None
    if len(raw)>2*1024**2:raise RuntimeError('API 响应超过上限')
    try:return json.loads(raw)
    except (ValueError,UnicodeError):raise RuntimeError('API 未返回 JSON') from None


def remote_models(base,key):
    data=remote(base,key,'/models')
    rows=data.get('data',[]) if isinstance(data,dict) else []
    models={row['id'] for row in rows if isinstance(row,dict) and isinstance(row.get('id'),str)}
    if not models:raise RuntimeError('/models 没有返回可用模型 ID')
    return models


def probe_chat(base,key,model):
    data=remote(base,key,'/chat/completions',{'model':model,'stream':False,'max_tokens':256,'messages':[{'role':'user','content':'Reply with OK.'}]})
    try:text=data['choices'][0]['message']['content']
    except (KeyError,IndexError,TypeError):raise RuntimeError('模型未返回兼容的聊天响应') from None
    if not isinstance(text,str) or not text.strip():raise RuntimeError('模型没有返回可见文本，请检查推理和输出参数')


def configure_providers(conf,env):
    context=int(env.get('CONTEXT_TOKENS',32768))
    if not 4096<=context<=1048576:raise ValueError('CONTEXT_TOKENS 超出允许范围')
    sources=[];providers=[]
    for name,model,timeout,output in [('chat',env['CHAT_MODEL'],110,int(env.get('CHAT_MAX_TOKENS',1536))),('deep',env['DEEP_MODEL'],480,int(env.get('DEEP_MAX_TOKENS',8192))),('classifier',env['CLASSIFIER_MODEL'],8,512)]:
        if not model:raise ValueError('模型 ID 不能为空')
        sources.append({'id':'api_'+name+'_source','provider':'openai','type':'openai_chat_completion','provider_type':'chat_completion','enable':True,'key':['${API_KEY}'],'api_base':env['API_BASE_URL'],'timeout':timeout,'custom_headers':{}})
        vision=env.get('CLASSIFIER_VISION_ENABLED' if name=='classifier' else 'VISION_ENABLED','false').lower()=='true'
        providers.append({'id':'api_'+name,'provider_source_id':'api_'+name+'_source','model':model,'enable':True,'provider_type':'chat_completion','type':'openai_chat_completion','modalities':['text','image'] if vision else ['text'],'max_context_tokens':min(context,16384) if name=='classifier' else context,'custom_extra_body':{'max_tokens':output}})
    conf['provider_sources']=sources;conf['provider']=providers
    conf['provider_settings'].update(enable=True,streaming_response=False)
    conf['agent_runner']['config']['model'].update(provider_id='api_chat',fallback_provider_ids=[],request_max_retries=1)
    conf['agent_runner']['config']['compression']['fallback_max_tokens']=context


def install_routing(env):
    source=ROOT/'plugins/astrbot_plugin_qqbot_guard/routing.json'
    target=ROOT/'data/astrbot/plugins/astrbot_plugin_qqbot_guard/routing.json'
    # Existing private tuning is preserved on subsequent apply operations.
    routing=json.loads((target if target.exists() else source).read_text())
    routing.update(default_model=env['CHAT_MODEL'],deep_model=env['DEEP_MODEL'],owner_qq=env['ADMIN_QQ'])
    for kind,prefix,path in [('image_generation','IMAGE','images/generations'),('voice_input','AUDIO','audio/transcriptions')]:
        settings=routing[kind];base=env.get(prefix+'_API_BASE_URL') or env['API_BASE_URL']
        settings.update(enabled=bool(env.get(prefix+'_MODEL')),model=env.get(prefix+'_MODEL',''))
        if kind=='image_generation':settings['endpoint']=base.rstrip('/')+'/'+path
        else:settings.update(api_base=base,api_key_env='AUDIO_API_KEY')
    save(target,routing)


def initialize(args):
    admin=args.admin or input("管理员 QQ：").strip()
    if (ROOT / ".env").exists() or (ROOT / "data").exists():
        raise RuntimeError("发现已有 .env 或 data，停止初始化以免覆盖。")
    if admin and not admin.isdigit():
        raise ValueError("管理员 QQ 必须是数字")
    base=input("OpenAI 兼容 API Base URL（包含 /v1）：").strip().rstrip('/')
    validate_base(base)
    key=getpass.getpass("API Key（隐藏输入）：").strip()
    if not key:raise ValueError("API Key 不能为空")
    models=remote_models(base,key)
    print("接口返回的模型："+", ".join(sorted(models)))
    chat=input("日常聊天模型 ID：").strip()
    deep=input("深度任务模型 ID（留空复用日常）：").strip() or chat
    classifier=input("意图判断小模型 ID（留空复用日常）：").strip() or chat
    chosen={chat,deep,classifier}
    if not chat or not chosen<=models:raise ValueError("所选模型不在 /models 的返回列表中")
    for model in sorted(chosen):probe_chat(base,key,model)
    lock = json.loads((ROOT / "images.lock.json").read_text())
    values = {
        "ASTRBOT_IMAGE": lock["astrbot"]["image"],
        "NAPCAT_IMAGE": lock["napcat"]["image"],
        "API_BASE_URL":base,"API_KEY":key,
        "CHAT_MODEL":chat,"DEEP_MODEL":deep,"CLASSIFIER_MODEL":classifier,
        "CONTEXT_TOKENS":"32768","CHAT_MAX_TOKENS":"1536","DEEP_MAX_TOKENS":"8192",
        "VISION_ENABLED":"false","CLASSIFIER_VISION_ENABLED":"false",
        "IMAGE_API_BASE_URL":"","IMAGE_API_KEY":"","IMAGE_MODEL":"",
        "AUDIO_API_BASE_URL":"","AUDIO_API_KEY":"","AUDIO_MODEL":"",
        "NAPCAT_WEBUI_TOKEN": secrets.token_hex(32),
        "ONEBOT_TOKEN": secrets.token_hex(32),
        "ASTRBOT_WEB_USERNAME": "astrbot",
        "ASTRBOT_WEB_PASSWORD": "Ab7" + secrets.token_hex(24),
        "ADMIN_QQ": admin,
    }
    env_write(values)
    for path in ("data/astrbot", "data/napcat/config", "data/napcat/qq", "data/napcat/plugins", ".state", "backups"):
        (ROOT / path).mkdir(parents=True, exist_ok=True, mode=0o700)
    salt = secrets.token_hex(16)
    pwd = values["ASTRBOT_WEB_PASSWORD"]
    digest = hashlib.pbkdf2_hmac("sha256", pwd.encode(), bytes.fromhex(salt), 600000).hex()
    astrbot = {
        "config_version": 5,
        "platform_settings": {
            "unique_session": True,
            "friend_message_needs_wake_prefix": False,
            "enable_id_white_list": False,
            "empty_mention_waiting": False,
            "empty_mention_waiting_need_reply": False,
            "ignore_at_all": True,
            "ignore_bot_self_message": True,
            "rate_limit": {"time": 60, "count": 10, "strategy": "discard"},
            "segmented_reply": {"enable": False},
        },
        "platform": [{"id": "napcat", "type": "aiocqhttp", "enable": True, "ws_reverse_host": "0.0.0.0", "ws_reverse_port": 6199, "ws_reverse_token": values["ONEBOT_TOKEN"]}],
        "wake_prefix": ["/", "小助手"],
        "admins_id": [admin] if admin else [],
        "provider_sources": [], "provider": [],
        "provider_settings": {
            "enable": False,
            "web_search": False,
            "reachability_check": False,
            "streaming_response": False,
            "unsupported_streaming_strategy": "turn_off",
            "display_reasoning_text": False,
            "file_extract": {"enable": False},
            "computer_use_runtime": "none",
            "proactive_capability": {"add_cron_tools": False},
            "show_tool_use_status": False,
        },
        "agent_runner": {
            "runner_type": "local",
            "config": {
                "model": {"provider_id": "", "fallback_provider_ids": [], "request_max_retries": 1},
                "persona": {"persona_id": "qqbot_chat", "safety_mode": True, "safety_mode_strategy": "system_prompt"},
                "compression": {"max_turns": 60, "trim_turns": 10, "overflow_strategy": "truncate_by_turns", "fallback_max_tokens": 32768},
                "misc": {"max_steps": 1, "tool_call_timeout": 10},
            },
        },
        "provider_ltm_settings": {"group_icl_enable": False, "group_message_history_enable": False, "active_reply": {"enable": False}},
        "provider_stt_settings": {"enable": False},
        "provider_tts_settings": {"enable": False},
        "subagent_orchestrator": {"main_enable": False, "agents": []},
        "dashboard": {
            "enable": True, "host": "0.0.0.0", "port": 6185,
            "username": values["ASTRBOT_WEB_USERNAME"],
            "password": hashlib.md5(pwd.encode()).hexdigest(),
            "pbkdf2_password": f"pbkdf2_sha256$600000${salt}${digest}",
            "password_storage_upgraded": True, "password_change_required": False,
            "jwt_secret": secrets.token_hex(32), "disable_access_log": True,
            "trust_proxy_headers": False,
        },
        "kb_names": [], "kb_agentic_mode": False,
        "t2i": False, "trace_enable": False, "trace_log_enable": False,
        "log_level": "INFO", "log_file_enable": False, "temp_dir_max_size": 128,
        "disable_metrics": True, "timezone": "Asia/Shanghai",
    }
    configure_providers(astrbot,values)
    save(ROOT / "data/astrbot/cmd_config.json", astrbot)
    nap = {
        "network": {"httpServers": [], "httpSseServers": [], "httpClients": [], "websocketServers": [],
                    "websocketClients": [{"enable": True, "name": "astrbot", "url": "ws://astrbot:6199/ws", "reportSelfMessage": False,
                                          "messagePostFormat": "array", "token": values["ONEBOT_TOKEN"], "debug": False,
                                          "heartInterval": 30000, "reconnectInterval": 5000}], "plugins": []},
        "musicSignUrl": "", "enableLocalFile2Url": False, "parseMultMsg": False,
    }
    save(ROOT / "data/napcat/config/onebot11.json", nap)
    save(ROOT / "data/napcat/config/webui.json", {"host": "0.0.0.0", "port": 6099, "token": values["NAPCAT_WEBUI_TOKEN"], "loginRate": 3})
    save(ROOT / "data/napcat/config/napcat.json", {"fileLog": False, "consoleLog": True, "consoleLogLevel": "warn"})
    shutil.copytree(ROOT / "plugins/astrbot_plugin_qqbot_guard", ROOT / "data/astrbot/plugins/astrbot_plugin_qqbot_guard")
    install_routing(values)
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        for item in (ROOT / "data/napcat").rglob("*"):
            os.chown(item, 1000, 1000)
        os.chown(ROOT / "data/napcat", 1000, 1000)
    save(ROOT / ".state/initialized.json", {"created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "credentials_printed": False})
    print("初始化完成，API 已通过最小文本请求；凭据保存在本机 .env（0600）。启动容器后仍须创建人设和手动扫码 QQ。")

def api(path, body=None, token=None, method=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request("http://127.0.0.1:6185/api/v1" + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            obj = json.load(r)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"AstrBot API HTTP {exc.code}；未输出响应体") from None
    if obj.get("status") != "ok":
        raise RuntimeError("AstrBot API 未返回成功；未输出可能含敏感信息的响应体")
    return obj.get("data")

def persona(update=False):
    env = env_read()
    session = api("/auth/login", {"username": env["ASTRBOT_WEB_USERNAME"], "password": env["ASTRBOT_WEB_PASSWORD"]})
    token = session["token"]
    personas = api("/personas", token=token)
    item = next((p for p in personas if p.get("persona_id") == "qqbot_chat"), None)
    if item is None:
        api("/personas", {"persona_id": "qqbot_chat", "system_prompt": (ROOT / "system-prompt.txt").read_text(),
                           "begin_dialogs": [], "tools": [], "skills": [],
                           "custom_error_message": "暂时无法取得模型回答，请稍后再试。"}, token=token)
    elif update:
        previous = api("/personas/by-id?persona_id=qqbot_chat", token=token)
        save(ROOT / "backups" / ("persona-before-update-" + time.strftime("%Y%m%d-%H%M%S") + ".json"), previous)
        api("/personas/by-id", {"persona_id": "qqbot_chat", "system_prompt": (ROOT / "system-prompt.txt").read_text(),
                              "tools": [], "skills": []}, token=token, method="PUT")
    detail = api("/personas/by-id?persona_id=qqbot_chat", token=token)
    if update and detail.get("system_prompt", "").strip() != (ROOT / "system-prompt.txt").read_text().strip():
        raise RuntimeError("人设提示词写入后核对失败。")
    if detail.get("tools") != [] or detail.get("skills") != []:
        raise RuntimeError("人设工具/技能未确认关闭，请在 WebUI 检查。")
    print("已确认 qqbot_chat 人设" + ("已更新，" if update else "已创建，") + "工具与技能列表均为空。")

def apply():
    result=subprocess.run(['docker','compose','--project-directory',str(ROOT),'ps','--services','--status','running'],capture_output=True,text=True,check=True)
    if 'astrbot' in result.stdout.splitlines():raise RuntimeError('请先 docker compose stop astrbot，避免运行中的配置写入冲突')
    env=env_read();validate_base(env['API_BASE_URL'])
    models=remote_models(env['API_BASE_URL'],env['API_KEY'])
    chosen={env['CHAT_MODEL'],env['DEEP_MODEL'],env['CLASSIFIER_MODEL']}
    if not chosen<=models:raise ValueError('配置的聊天模型不在 /models 中')
    for model in sorted(chosen):probe_chat(env['API_BASE_URL'],env['API_KEY'],model)
    # Stop AstrBot before this command; no process may rewrite these files concurrently.
    path=ROOT/'data/astrbot/cmd_config.json'
    conf=json.loads(path.read_text(encoding='utf-8-sig'))
    backup=ROOT/'backups'/('before-api-'+time.strftime('%Y%m%d-%H%M%S'));backup.mkdir(parents=True)
    save(backup/'cmd_config.json',conf)
    configure_providers(conf,env);save(path,conf);install_routing(env)
    print('API 验证通过并写入；请启动 AstrBot。原配置已备份。')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['init','models','probe','persona','apply'])
    parser.add_argument('--admin',default='')
    parser.add_argument('--update',action='store_true')
    args=parser.parse_args()
    try:
        if args.command=='init':initialize(args)
        elif args.command=='persona':persona(args.update)
        elif args.command=='apply':apply()
        else:
            env=env_read();validate_base(env['API_BASE_URL'])
            if args.command=='models':print('\n'.join(sorted(remote_models(env['API_BASE_URL'],env['API_KEY']))))
            else:
                for model in sorted({env['CHAT_MODEL'],env['DEEP_MODEL'],env['CLASSIFIER_MODEL']}):probe_chat(env['API_BASE_URL'],env['API_KEY'],model)
                print('配置的三个聊天角色均返回可见文本。')
    except (RuntimeError,ValueError,FileNotFoundError) as error:
        print(str(error),file=sys.stderr);return 1
    return 0


if __name__=='__main__':sys.exit(main())
