"""Trusted QQ object metadata, with stable scoped references instead of nicknames."""
import hashlib


def reference(scope, identifier, prefix='成员'):
    if identifier in (None, '', '0'):
        return '未知'
    return prefix+'-'+hashlib.sha256((str(scope)+':'+str(identifier)).encode()).hexdigest()[:12]


def scope(event):
    return str(event.get_platform_id())+':'+str(getattr(event,'get_group_id',lambda:None)() or event.get_sender_id())


def message_identity(event, *, bot=False):
    key=scope(event)
    self_id=str(getattr(event,'get_self_id',lambda:None)() or '')
    sender=self_id if bot else str(event.get_sender_id() or '')
    mid=getattr(event.message_obj,'message_id',None)
    current_ref=reference(key,mid,'消息')
    return {'speaker_ref':reference(key,sender),'speaker_name':'小助手' if bot else str(getattr(event,'get_sender_name',lambda:None)() or '群成员')[:40],
            'is_bot':bool(bot or self_id and sender==self_id),
            'message_ref':reference(key,'reply:'+str(mid),'回复') if bot and mid else current_ref,
            'in_reply_to':current_ref if bot else None}
