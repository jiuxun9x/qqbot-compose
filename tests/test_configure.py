"""Non-overwrite, secret handling and standard API contract checks."""
from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

spec=importlib.util.spec_from_file_location('qqbot_configure',Path(__file__).resolve().parents[1]/'scripts/configure.py')
config=importlib.util.module_from_spec(spec);spec.loader.exec_module(config)


class ConfigureTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.patch=patch.object(config,'ROOT',self.root);self.patch.start();self.addCleanup(self.patch.stop)

    def test_credentials_round_trip_without_interpolation_and_are_private(self):
        values={'API_KEY':"fixture-$dollar#hash'quote",'CHAT_MODEL':'fixture-model'}
        config.env_write(values)
        self.assertEqual(config.env_read(),values)
        self.assertEqual((self.root/'.env').stat().st_mode&0o777,0o600)
        for unsafe in ('one\ntwo','one\rtwo','one\x00two'):
            with self.assertRaises(ValueError):config.env_write({'API_KEY':unsafe})

    def test_init_refuses_existing_data_or_env_before_network_or_prompts(self):
        args=type('Args',(),{'admin':'10001'})()
        for name in ('data','.env'):
            target=self.root/name
            if name=='data':target.mkdir()
            else:target.write_text('private existing value')
            with patch('builtins.input',side_effect=AssertionError('must not ask')):
                with self.assertRaisesRegex(RuntimeError,'已有'):config.initialize(args)
            if name=='data':target.rmdir()
            else:self.assertEqual(target.read_text(),'private existing value');target.unlink()

    def test_init_generates_matching_internal_tokens_but_never_prints_credentials(self):
        import shutil
        source=Path(__file__).resolve().parents[1]
        shutil.copyfile(source/'images.lock.json',self.root/'images.lock.json')
        shutil.copytree(source/'plugins',self.root/'plugins')
        secret="fixture-api-secret-$#'"
        args=type('Args',(),{'admin':'10001'})()
        output=io.StringIO()
        with patch('builtins.input',side_effect=['https://api.example.com/v1','fixture-chat','','fixture-small']),patch.object(config.getpass,'getpass',return_value=secret),patch.object(config,'remote_models',return_value={'fixture-chat','fixture-small'}),patch.object(config,'probe_chat') as probe,patch.object(config.os,'chown'),redirect_stdout(output):
            config.initialize(args)
        self.assertEqual(probe.call_count,2)
        env=config.env_read();conf=json.loads((self.root/'data/astrbot/cmd_config.json').read_text())
        nap=json.loads((self.root/'data/napcat/config/onebot11.json').read_text())
        routing=json.loads((self.root/'data/astrbot/plugins/astrbot_plugin_qqbot_guard/routing.json').read_text())
        self.assertEqual(env['API_KEY'],secret)
        self.assertEqual(conf['platform'][0]['ws_reverse_token'],nap['network']['websocketClients'][0]['token'])
        self.assertEqual(conf['agent_runner']['config']['model']['provider_id'],'api_chat')
        self.assertEqual(routing['default_model'],'fixture-chat')
        self.assertEqual(routing['group_reply']['probability'],0.05)
        self.assertEqual(routing['group_reply']['cooldown_seconds'],5)
        self.assertFalse(routing['image_generation']['enabled'])
        self.assertFalse(routing['voice_input']['enabled'])
        for key in ('API_KEY','ONEBOT_TOKEN','ASTRBOT_WEB_PASSWORD','NAPCAT_WEBUI_TOKEN'):
            self.assertNotIn(env[key],output.getvalue())
        self.assertEqual(conf['provider_sources'][0]['key'],['${API_KEY}'])
        # Before the QQ login step, no external messages have been sent.

    def test_provider_creation_uses_user_models_and_keeps_classification_budget_small(self):
        env={'API_BASE_URL':'https://api.example.com/v1','CHAT_MODEL':'actual-chat','DEEP_MODEL':'actual-deep','CLASSIFIER_MODEL':'actual-small','CONTEXT_TOKENS':'204800','VISION_ENABLED':'true'}
        conf={'provider_settings':{},'agent_runner':{'config':{'model':{},'compression':{}}}}
        config.configure_providers(conf,env)
        rows=conf['provider'];self.assertEqual([row['model'] for row in rows],['actual-chat','actual-deep','actual-small'])
        self.assertEqual(rows[0]['max_context_tokens'],204800)
        self.assertEqual(rows[-1]['max_context_tokens'],16384)
        self.assertEqual(rows[0]['modalities'],['text','image'])
        self.assertEqual(rows[-1]['modalities'],['text'])
        self.assertFalse(any('thinking' in row['custom_extra_body'] for row in rows))


class APIContractTests(unittest.TestCase):
    def setUp(self):
        self.requests=[];self.mode='ok';outer=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                outer.requests.append((self.path,self.headers.get('Authorization')))
                if outer.mode=='redirect':
                    self.send_response(302);self.send_header('Location','http://127.0.0.1:1/leak');self.end_headers();return
                if outer.mode=='failure':
                    self.send_response(401);self.end_headers();self.wfile.write(b'private-upstream-secret');return
                self.send_response(200);self.end_headers();self.wfile.write(json.dumps({'data':[{'id':'real-model'}]}).encode())
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                outer.requests.append((self.path,body))
                self.send_response(200);self.end_headers();self.wfile.write(json.dumps({'choices':[{'message':{'content':'OK'}}]}).encode())
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.base='http://127.0.0.1:'+str(self.server.server_port)+'/v1'

    def test_models_and_chat_use_the_supplied_base_and_model(self):
        self.assertEqual(config.remote_models(self.base,'fixture-secret'),{'real-model'})
        config.probe_chat(self.base,'fixture-secret','real-model')
        self.assertEqual(self.requests[0],('/v1/models','Bearer fixture-secret'))
        self.assertEqual(self.requests[1][0],'/v1/chat/completions')
        self.assertEqual(self.requests[1][1]['model'],'real-model')
        self.assertFalse(self.requests[1][1]['stream'])

    def test_redirect_is_refused_and_error_body_is_not_disclosed(self):
        for mode in ('redirect','failure'):
            self.mode=mode
            with self.assertRaises(RuntimeError) as error:config.remote_models(self.base,'fixture-secret')
            self.assertNotIn('private-upstream-secret',str(error.exception))
            self.assertNotIn('fixture-secret',str(error.exception))


if __name__=='__main__':unittest.main()
