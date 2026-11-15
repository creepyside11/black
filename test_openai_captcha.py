import base64
import json
import os
import unittest
from unittest.mock import patch

import httpx
from openai import OpenAI
from logic.captcha import KIND_IMAGE_URL, CaptchaChallenge, CaptchaAnswerer, default_chain
from logic.openai_captcha import solve_bytes, OpenAICaptchaSolver

PNG = b'\x89PNG\r\n\x1a\n' + b'\0' * 32

class VisionTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.logs = []
        self.response = {'id':'1', 'object':'chat.completion', 'created':1, 'model':'vision-model',
            'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':'aB29'}}]}

    def factory(self, **kwargs):
        def handle(req):
            self.calls.append(req)
            return httpx.Response(200, json=self.response)
        return OpenAI(**kwargs, http_client=httpx.Client(transport=httpx.MockTransport(handle)))

    def solve(self, data=PNG, **kwargs):
        return solve_bytes(data, base_url='https://example.com/v1', api_token='private-test-key',
                           model='vision-model', log=self.logs.append, client_factory=self.factory, **kwargs)

    def test_real_sdk_request_structure(self):
        self.assertEqual(self.solve(), 'aB29')
        request = self.calls[0]
        self.assertEqual(str(request.url),'https://example.com/v1/chat/completions')
        self.assertEqual(request.headers['authorization'], 'Bearer private-test-key')
        data = json.loads(request.content)
        self.assertEqual(data['model'],'vision-model')
        content = data['messages'][0]['content']
        self.assertEqual(content[0]['type'],'text')
        self.assertEqual(content[1]['type'],'image_url')
        self.assertEqual(content[1]['image_url']['url'], 'data:image/png;base64,' + base64.b64encode(PNG).decode())

    def test_unreadable_and_empty_choices(self):
        self.response['choices'][0]['message']['content']='UNREADABLE'
        self.assertIsNone(self.solve())
        self.response['choices']=[]
        self.assertIsNone(self.solve())

    def test_incorrect_response_and_refusal(self):
        self.response['choices'][0]['message']['content']='Ответ: aB29'
        self.assertIsNone(self.solve())
        self.response['choices'][0]['message']['content']='aB29'
        self.response['choices'][0]['message']['refusal']='no'
        self.assertIsNone(self.solve())

    def test_no_credentials_no_http_or_unknown_image(self):
        self.assertIsNone(self.solve(b'not an image'))
        self.assertIsNone(solve_bytes(PNG,base_url='https://example.com/v1',api_token='',model='vision',log=self.logs.append,client_factory=self.factory))
        self.assertIsNone(solve_bytes(PNG,base_url='http://example.com/v1',api_token='key',model='vision',log=self.logs.append,client_factory=self.factory))
        self.assertEqual(self.calls,[])

    def test_provider_error_never_logs_secrets(self):
        def broken(**kwargs): raise RuntimeError('private-test-key')
        self.assertIsNone(solve_bytes(PNG,base_url='https://example.com/v1',api_token='private-test-key',model='vision',log=self.logs.append,client_factory=broken))
        self.assertNotIn('private-test-key',str(self.logs))

    def test_auth_failure_does_not_leak_key(self):
        def factory(**kwargs):
            return OpenAI(**kwargs, http_client=httpx.Client(transport=httpx.MockTransport(
                lambda req: httpx.Response(401,json={'error': {'message':'private-test-key', 'type':'authentication_error'}}))))
        result = solve_bytes(PNG,base_url='https://example.com/v1',api_token='private-test-key',
                             model='vision',log=self.logs.append,client_factory=factory)
        self.assertIsNone(result)
        self.assertNotIn('private-test-key',str(self.logs))
        self.assertIn('401',str(self.logs))

    def test_answerer_sends_recognized_value(self):
        from unittest.mock import Mock
        challenge=CaptchaChallenge(KIND_IMAGE_URL,image=PNG,image_url='https://example.com/c.png')
        session=Mock()
        session.send_json.return_value=True
        with patch('logic.openai_captcha.solve_bytes',return_value='aB29'):
            report=CaptchaAnswerer(chain=default_chain(),delay=(0,0)).handle(session,challenge)
        self.assertTrue(report['sent'])
        session.send_json.assert_called_once_with(26,{'s':'aB29','t':0})

    def test_solver_chain_and_image_reuse(self):
        challenge=CaptchaChallenge(KIND_IMAGE_URL,image=PNG,image_url='https://example.com/c.png')
        with patch('logic.openai_captcha.solve_bytes',return_value='aB29') as solve:
            self.assertEqual(OpenAICaptchaSolver().solve(challenge),'aB29')
            self.assertEqual(solve.call_args.args[0],PNG)
        chain=default_chain()
        self.assertEqual([item.name for item in chain.solvers],['packet_text','openai_vision'])

if __name__ == '__main__': unittest.main()
