"""Long math answers keep all rendered pages and report limits precisely."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_guard as base
import test_renderer


class LongRenderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        base.GuardTests.setUp(self)

    def event(self):
        event = base.Event(text='请做这份试卷')
        event.set_extra('qqbot_route', {'decision': 'hard'})
        event.set_extra('qqbot_presentation', 'latex')
        event.result = SimpleNamespace(chain=[base.guard_module.Plain('完整数学答案 $x=1$')],
                                       is_llm_result=lambda: True)
        return event

    async def test_eighteen_pages_are_kept_in_order(self):
        images = [base.GuardTests._image_fixture() for _ in range(18)]
        async def render(*args, **kwargs):
            return {'pages': images, 'elapsed_ms': 123}
        self.guard._post_json = render
        event = self.event()
        await self.guard.present_answer(event)
        self.assertEqual(len(event.result.chain), 18)
        self.assertEqual(event.get_extra('qqbot_rendered')['pages'], 18)
        self.assertFalse(event.get_extra('qqbot_render_failed'))

    async def test_twenty_four_pages_allowed_but_excess_rejected(self):
        for count in (24, 25):
            async def render(*args, **kwargs):
                return {'pages': [base.GuardTests._image_fixture()] * count}
            self.guard._post_json = render
            event = self.event()
            await self.guard.present_answer(event)
            if count == 24:
                self.assertEqual(event.get_extra('qqbot_rendered')['pages'], 24)
            else:
                self.assertTrue(event.get_extra('qqbot_render_failed'))
                self.assertEqual(len(event.result.chain), 1)
                self.assertNotIn('$', event.result.chain[0].text)

    async def test_length_limits_explain_split_instead_of_repeating_same_retry(self):
        for code in ('page_limit_exceeded', 'document_too_long', 'image_limit_exceeded'):
            async def render(*args, **kwargs):
                raise base.guard_module.RenderRequestError(code)
            self.guard._post_json = render
            event = self.event()
            await self.guard.present_answer(event)
            self.assertEqual(event.get_extra('qqbot_render_error_code'), code)
            self.assertIn('答案已生成', event.result.chain[0].text)
            self.assertIn('分开发送', event.result.chain[0].text)
            self.assertNotIn('稍后重试', event.result.chain[0].text)

    async def test_known_renderer_error_is_read_with_a_small_bound(self):
        reads = []
        async def read(size):
            reads.append(size)
            return json.dumps({'error': 'page_limit_exceeded'}).encode()
        response = AsyncContext(status=422, content=SimpleNamespace(read=read))
        session = AsyncContext(post=lambda *a, **kw: response)
        with patch.object(base.guard_module.aiohttp, 'ClientSession', return_value=session):
            with self.assertRaises(base.guard_module.RenderRequestError) as caught:
                await base.guard_module.QQBotGuard._post_json('http://renderer:8080/render', {}, 65, 20*1024**2)
        self.assertEqual(caught.exception.code, 'page_limit_exceeded')
        self.assertEqual(reads, [4096])

    async def test_provider_error_body_is_not_exposed(self):
        for payload in ({'error': {'message': 'PRIVATE_SECRET_TEST'}},
                        {'error': 'PRIVATE_SECRET_TEST'}, ['PRIVATE_SECRET_TEST']):
            async def read(size):
                return json.dumps(payload).encode()
            response = AsyncContext(status=503, content=SimpleNamespace(read=read))
            session = AsyncContext(post=lambda *a, **kw: response)
            with patch.object(base.guard_module.aiohttp, 'ClientSession', return_value=session):
                with self.assertRaises(RuntimeError) as caught:
                    await base.guard_module.QQBotGuard._post_json('http://gateway/v1/images/generations', {}, 150, 24*1024**2)
            self.assertEqual(str(caught.exception), 'api_status_503')

    def test_client_and_renderer_have_the_same_page_bound(self):
        self.assertEqual(test_renderer.renderer.MAX_PAGES, 24)
        self.assertEqual(base.guard_module.MAX_RENDER_PAGES, 24)


class AsyncContext:
    def __init__(self, **values):
        self.__dict__.update(values)
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass
