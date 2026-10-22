"""Voice size/duration/silence/privacy boundaries and structured recognition."""
import asyncio
import base64
import json
import math
import os
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

from aiohttp import web
import test_guard as base

voice=__import__(base.guard_module.__package__+'.voice',fromlist=['*'])


def make_wave(path,seconds=0.5,silent=False):
    with wave.open(str(path),'wb') as stream:
        stream.setparams((1,2,16000,0,'NONE','NONE'))
        stream.writeframes(b''.join(struct.pack('<h',0 if silent else int(5000*math.sin(index*2*math.pi*440/16000))) for index in range(int(seconds*16000))))


class VoiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.reader=voice.VoiceInput(self.root/'voice',trusted_roots=[self.root/'trusted'])
        self.trusted=self.root/'trusted';self.trusted.mkdir()

    async def test_opaque_onebot_id_requests_mp3_and_reads_base64(self):
        file=self.trusted/'a.wav';make_wave(file)
        calls=[]
        async def call(action,**kw):calls.append((action,kw));return {'base64':base64.b64encode(file.read_bytes()).decode()}
        audio=await self.reader.prepare(SimpleNamespace(bot=SimpleNamespace(call_action=call)),SimpleNamespace(file='opaque.amr'))
        self.assertEqual(calls,[('get_record',{'file':'opaque.amr','out_format':'mp3'})])
        self.assertAlmostEqual(audio['duration_seconds'],0.5,places=2)
        self.reader.cleanup(audio)
        self.assertFalse(Path(audio['job']).exists())

    async def test_original_silk_is_decoded_with_existing_codec(self):
        import pilk
        file=self.trusted/'a.wav';make_wave(file)
        pcm=self.trusted/'a.pcm'
        with wave.open(str(file),'rb') as audio:pcm.write_bytes(audio.readframes(audio.getnframes()))
        silk=self.trusted/'a.silk';pilk.encode(str(pcm),str(silk),pcm_rate=16000,tencent=True)
        audio=await self.reader.prepare(SimpleNamespace(),SimpleNamespace(file=str(silk)))
        self.assertGreater(audio['duration_seconds'],0.3)
        self.reader.cleanup(audio)

    async def test_framework_normalized_missing_path_recovers_original_OneBot_id(self):
        file=self.trusted/'a.wav';make_wave(file);calls=[]
        record=SimpleNamespace(file='/AstrBot/original.amr')
        async def call(action,**kw):calls.append((action,kw));return {'base64':base64.b64encode(file.read_bytes()).decode()}
        event=SimpleNamespace(bot=SimpleNamespace(call_action=call),get_messages=lambda:[record],
            message_obj=SimpleNamespace(raw_message={'message':[{'type':'record','data':{'file':'original.amr'}}]}))
        audio=await self.reader.prepare(event,record)
        self.assertEqual(calls,[('get_record',{'file':'original.amr','out_format':'mp3'})]);self.reader.cleanup(audio)

    async def test_quoted_normalized_path_recovers_verified_original_message(self):
        file=self.trusted/'a.wav';make_wave(file);calls=[]
        record=SimpleNamespace(file='/AstrBot/quoted.amr');quote=SimpleNamespace(id='73',sender_id='12',chain=[record])
        async def call(action,**kw):
            calls.append((action,kw))
            if action=='get_msg':return {'message_id':73,'user_id':12,'message':[{'type':'record','data':{'file':'quoted.amr'}}]}
            return {'base64':base64.b64encode(file.read_bytes()).decode()}
        event=SimpleNamespace(bot=SimpleNamespace(call_action=call),get_messages=lambda:[quote])
        audio=await self.reader.prepare(event,record)
        self.assertEqual(calls,[('get_msg',{'message_id':73}),('get_record',{'file':'quoted.amr','out_format':'mp3'})]);self.reader.cleanup(audio)

    async def test_quoted_original_message_with_wrong_author_is_rejected(self):
        record=SimpleNamespace(file='/AstrBot/quoted.amr');quote=SimpleNamespace(id='73',sender_id='12',chain=[record])
        async def call(action,**kw):
            self.assertEqual(action,'get_msg')
            return {'message_id':73,'user_id':99,'message':[{'type':'record','data':{'file':'quoted.amr'}}]}
        with self.assertRaisesRegex(voice.VoiceError,'voice_download_failed'):
            await self.reader.prepare(SimpleNamespace(bot=SimpleNamespace(call_action=call),get_messages=lambda:[quote]),record)

    async def test_digital_silence_rejected_before_remote_recognition(self):
        file=self.trusted/'silent.wav';make_wave(file,silent=True)
        with self.assertRaisesRegex(voice.VoiceError,'voice_silent'):
            await self.reader.prepare(SimpleNamespace(),SimpleNamespace(file=str(file)))
        self.assertFalse(list(self.reader.folder.glob('*/audio.wav')))

    async def test_over_one_minute_rejected_without_partial_transcript(self):
        file=self.trusted/'long.wav';make_wave(file,60.5)
        with self.assertRaisesRegex(voice.VoiceError,'voice_duration_limit'):
            await self.reader.prepare(SimpleNamespace(),SimpleNamespace(file=str(file)))

    async def test_unknown_local_path_cannot_read_other_application_files(self):
        file=self.root/'outside.wav';make_wave(file)
        with self.assertRaisesRegex(voice.VoiceError,'voice_download_failed'):
            await self.reader.prepare(SimpleNamespace(),SimpleNamespace(file=str(file)))

    async def test_symlink_to_untrusted_audio_is_rejected(self):
        file=self.root/'outside.wav';make_wave(file)
        link=self.trusted/'link.wav';link.symlink_to(file)
        with self.assertRaisesRegex(voice.VoiceError,'voice_download_failed'):
            await self.reader.prepare(SimpleNamespace(),SimpleNamespace(file=str(link)))

    async def test_private_url_cannot_be_used_for_voice_download(self):
        with self.assertRaises(Exception):
            await self.reader.prepare(SimpleNamespace(),SimpleNamespace(file='http://127.0.0.1:8081/health'))

    async def test_oversize_base64_rejected_before_decode(self):
        with self.assertRaisesRegex(voice.VoiceError,'voice_size_limit'):
            self.reader._decode_base64('A'*(6*1024**2))

    async def test_corrupt_audio_rejected_and_cache_removed(self):
        file=self.trusted/'a.bin';file.write_bytes(b'not audio')
        with self.assertRaisesRegex(voice.VoiceError,'voice_decode_failed'):
            await self.reader.prepare(SimpleNamespace(),SimpleNamespace(file=str(file)))
        self.assertEqual(list(self.reader.folder.iterdir()),[])

    async def test_failed_onebot_lookup_uses_real_public_url_fallback(self):
        async def call(*args,**kw):raise RuntimeError('fixture failure')
        source=self.trusted/'a.wav';make_wave(source)
        async def download(url,target):self.assertEqual(url,'https://example.com/audio');target.write_bytes(source.read_bytes())
        self.reader.downloader._download=download
        audio=await self.reader.prepare(SimpleNamespace(bot=SimpleNamespace(call_action=call)),SimpleNamespace(file='opaque.amr',url='https://example.com/audio'))
        self.assertGreater(audio['duration_seconds'],0.3);self.reader.cleanup(audio)

    async def test_cancelled_download_removes_own_job(self):
        entered=asyncio.Event()
        async def call(*args,**kw):entered.set();await asyncio.Event().wait()
        task=asyncio.create_task(self.reader.prepare(SimpleNamespace(bot=SimpleNamespace(call_action=call)),SimpleNamespace(file='opaque.amr')))
        await entered.wait();task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(list(self.reader.folder.iterdir()),[])

    async def test_structured_json_required_and_non_speech_stays_empty(self):
        self.assertEqual(voice.parse_transcript('{"has_speech":true,"text":"你好"}'),'你好')
        for value in ('你好','{"has_speech":"true","text":"你好"}','{"has_speech":false,"text":"猜测"}','{"has_speech":true,"text":""}'):
            with self.assertRaises(voice.VoiceError):voice.parse_transcript(value)

    async def test_truncated_or_control_text_rejected(self):
        with self.assertRaises(voice.VoiceError):voice.parse_transcript(json.dumps({'has_speech':True,'text':'x'*2001}))
        with self.assertRaises(voice.VoiceError):voice.parse_transcript(json.dumps({'has_speech':True,'text':'hello\u0000'}))

    async def test_http_client_sends_audio_without_transcript_or_history(self):
        file=self.trusted/'a.wav';make_wave(file)
        seen=[]
        async def respond(request):
            body=await request.post();seen.append(dict(body))
            self.assertEqual(request.headers['Authorization'],'Bearer fixture-only')
            return web.json_response({'text':'识别结果'})
        app=web.Application();app.router.add_post('/v1/audio/transcriptions',respond)
        runner=web.AppRunner(app);await runner.setup();server=web.TCPSite(runner,'127.0.0.1',0);await server.start()
        port=server._server.sockets[0].getsockname()[1]
        try:
            with patch.dict(os.environ,{'VOICE_FIXTURE_KEY':'fixture-only'}):
                result=await self.reader.transcribe({'path':str(file)}, {'model':'fixture-model','api_base':f'http://127.0.0.1:{port}/v1','api_key_env':'VOICE_FIXTURE_KEY'})
            self.assertEqual(result,'识别结果')
            self.assertEqual(seen[0]['model'],'fixture-model')
            self.assertEqual(seen[0]['response_format'],'json')
            self.assertEqual(seen[0]['file'].filename,'audio.wav')
            self.assertEqual(set(seen[0]),{'model','response_format','file'})
        finally:await runner.cleanup()

    async def test_http_response_can_arrive_in_multiple_network_chunks(self):
        file=self.trusted/'a.wav';make_wave(file)
        async def respond(request):
            await request.read()
            response=web.StreamResponse(headers={'Content-Type':'application/json'})
            await response.prepare(request)
            payload=json.dumps({'text':'完整转写'}).encode()
            await response.write(payload[:30]);await asyncio.sleep(0.02)
            await response.write(payload[30:]);await response.write_eof()
            return response
        app=web.Application();app.router.add_post('/v1/audio/transcriptions',respond)
        runner=web.AppRunner(app);await runner.setup();server=web.TCPSite(runner,'127.0.0.1',0);await server.start()
        port=server._server.sockets[0].getsockname()[1]
        try:
            with patch.dict(os.environ,{'VOICE_FIXTURE_KEY':'fixture-only'}):
                result=await self.reader.transcribe({'path':str(file)}, {'model':'fixture-model','api_base':f'http://127.0.0.1:{port}/v1','api_key_env':'VOICE_FIXTURE_KEY'})
            self.assertEqual(result,'完整转写')
        finally:await runner.cleanup()
