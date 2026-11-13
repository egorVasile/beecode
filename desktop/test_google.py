import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).parent / 'agent-src'))
from beeagent import serve
from beeagent.providers import pool


class GoogleFlow(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.env = patch.dict(os.environ, {'BEECODE_POOL_KEY_FILE': str(Path(self.temp.name) / 'key.json')})
        self.env.start()
        self.old = serve._state.copy()
        self.config = SimpleNamespace(pool_url='https://pool.example', pool_token='')
        self.provider = SimpleNamespace(url='', token='')
        serve._state.update(config=self.config, agent=SimpleNamespace(
            providers=SimpleNamespace(get=lambda name: self.provider)))

    def tearDown(self):
        serve._state.update(self.old)
        self.env.stop()
        self.temp.cleanup()

    def action(self, action):
        handler = object.__new__(serve.Handler)
        with patch.object(serve, '_read_json', return_value={'action': action}), \
                patch.object(serve, '_send_json') as send:
            handler._pool()
            return send.call_args.args[1:]

    def test_authorize_public_identity_only(self):
        self.assertTrue(hasattr(pool, 'google_authorize_url'), 'Google helper missing')
        with patch.object(pool, 'install_key', return_value=(b's' * 32, 'ab' * 32, 'device')):
            url = pool.google_authorize_url('https://pool.example/')
        self.assertEqual(urlsplit(url).path, '/v1/google/authorize')
        self.assertEqual(parse_qs(urlsplit(url).query), {
            'state': [pool.install_id('ab' * 32)], 'pub': ['ab' * 32]})
        self.assertNotIn((b's' * 32).hex(), url)

    def test_google_start_does_not_save_unverified_seat(self):
        status, body = self.action('google')
        self.assertEqual(status, 200)
        self.assertIn('/v1/google/authorize?', body['url'])
        self.assertEqual(self.config.pool_token, '')

    def test_discovery_persists_and_refreshes(self):
        self.assertTrue(hasattr(pool, 'seat_status'), 'Seat polling helper missing')
        with patch.object(pool, 'seat_status', return_value={'ok': True, 'approved': True}), \
                patch('beeagent.config.loader.save_config') as save:
            status, body = self.action('google-status')
        self.assertEqual(status, 200)
        self.assertTrue(body['complete'])
        self.assertEqual(self.config.pool_token, pool.install_id())
        self.assertEqual(self.provider.token, self.config.pool_token)
        save.assert_called_once()

    def test_pending_and_refusal_do_not_save(self):
        self.assertTrue(hasattr(pool, 'seat_status'), 'Seat polling helper missing')
        for seat in ({'ok': False, 'status': 401, 'error': 'unknown token'},
                     {'ok': True, 'approved': False}):
            with patch.object(pool, 'seat_status', return_value=seat), \
                    patch('beeagent.config.loader.save_config') as save:
                status, body = self.action('google-status')
            self.assertFalse(body['complete'])
            save.assert_not_called()
        with patch.object(pool, 'seat_status', return_value={'ok': False, 'status': 403, 'error': 'refused'}):
            status, body = self.action('google-status')
        self.assertEqual(status, 400)
        with patch.object(pool, 'seat_status', return_value={
                'ok': False, 'status': 401, 'detail': 'prove this install holds the seat: bad signature',
                'error': 'bad signature'}):
            status, body = self.action('google-status')
        self.assertEqual(status, 400)

    def test_save_failure_rolls_back(self):
        self.assertTrue(hasattr(pool, 'seat_status'), 'Seat polling helper missing')
        with patch.object(pool, 'seat_status', return_value={'ok': True, 'approved': True}), \
                patch('beeagent.config.loader.save_config', side_effect=OSError('disk full')):
            status, body = self.action('google-status')
        self.assertEqual(status, 500)
        self.assertEqual(self.config.pool_token, '')
        self.assertEqual(self.provider.token, '')

    def test_seat_poll_signs_empty_body(self):
        self.assertTrue(hasattr(pool, 'seat_status'), 'Seat polling helper missing')
        with patch.object(pool.httpx, 'Client') as client, \
                patch.object(pool, 'install_key', return_value=(b's' * 32, 'ab' * 32, 'device')), \
                patch.object(pool, 'signed_headers', return_value={'X-Seat-Signature': 'sig'}) as sign:
            client.return_value.__enter__.return_value.get.return_value = SimpleNamespace(
                status_code=200, json=lambda: {'approved': True})
            self.assertTrue(pool.seat_status('https://pool.example', '#beecode123')['ok'])
            call = client.return_value.__enter__.return_value.get.call_args
        self.assertEqual(call.args[0], 'https://pool.example/v1/seat')
        self.assertEqual(call.kwargs['headers']['X-Bee-Id'], '#beecode123')
        sign.assert_called_once_with(b's' * 32, 'ab' * 32, b'')

    def test_real_config_persistence_and_current_pool_budget(self):
        from beeagent.config.schema import BeeConfig
        self.config = BeeConfig(pool_url='https://pool.example')
        serve._state.update(config=self.config, workdir=self.temp.name)
        with patch.object(pool, 'seat_status', return_value={
                'ok': True, 'approved': True, 'requests': 3, 'requests_limit': 20,
                'tokens': 100, 'tokens_limit': 1000}):
            status, body = self.action('google-status')
        self.assertEqual(status, 200)
        stored = json.loads((Path(self.temp.name) / 'beeagent.json').read_text(encoding='utf-8'))
        self.assertEqual(stored['pool_token'], pool.install_id())
        self.assertEqual(body['pool']['seat']['requests_left'], 17)
        self.assertEqual(body['pool']['seat']['tokens_left'], 900)


if __name__ == '__main__':
    unittest.main()
