"""No network required: run with python -m unittest -v test_bot.py"""
import tempfile
import unittest
from pathlib import Path
from bot import StarsBot, Storage


class FakeAPI:
    def __init__(self):
        self.calls = []

    def call(self, method, payload=None, timeout=30):
        self.calls.append((method, payload or {}))
        if method == "getMyStarBalance":
            return {"amount": 25}
        return {"message_id": 1}


class BotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Storage(Path(self.tmp.name) / 'db.sqlite')
        self.api = FakeAPI()
        self.bot = StarsBot(self.api, self.db, 123)
        self.item_id = self.db.add_item('Trial Photo', 'photo', 'valid-looking-file-id', 45)

    def tearDown(self):
        self.tmp.cleanup()

    def test_paid_media_and_purchase_confirm(self):
        self.bot.buy(456, 456, self.item_id)
        method, payload = self.api.calls[-1]
        self.assertEqual(method, 'sendPaidMedia')
        self.assertEqual(payload['star_count'], 45)
        self.assertEqual(payload['media'][0]['type'], 'photo')
        self.assertFalse(self.db.purchased(456, self.item_id))
        self.bot.purchased({'from': {'id': 456}, 'paid_media_payload': payload['payload']})
        self.bot.purchased({'from': {'id': 456}, 'paid_media_payload': payload['payload']})
        self.assertTrue(self.db.purchased(456, self.item_id))
        self.assertEqual(self.db.stats()[1:3], (1, 45))
        self.bot.again(456, 456, self.item_id)
        self.assertEqual(self.api.calls[-1][0], 'sendPhoto')

    def test_wrong_user_cannot_claim_purchase(self):
        self.bot.buy(456, 456, self.item_id)
        payload = self.api.calls[-1][1]['payload']
        self.bot.purchased({'from': {'id': 987}, 'paid_media_payload': payload})
        self.assertFalse(self.db.purchased(456, self.item_id))
        self.assertFalse(self.db.purchased(987, self.item_id))

    def test_admin_draft(self):
        self.bot.handle_message({'from': {'id': 123}, 'chat': {'id': 123, 'type': 'private'}, 'text': '/add'})
        self.bot.handle_message({'from': {'id': 123}, 'chat': {'id': 123, 'type': 'private'},
                                 'photo': [{'file_id': 'abc'}]})
        self.bot.handle_message({'from': {'id': 123}, 'chat': {'id': 123, 'type': 'private'}, 'text': 'Second Photo'})
        self.bot.handle_message({'from': {'id': 123}, 'chat': {'id': 123, 'type': 'private'}, 'text': '100'})
        self.assertEqual(len(self.db.catalog()), 2)
        self.assertEqual(self.db.catalog()[0]['price'], 100)

    def test_catalog_requires_existing_item(self):
        self.bot.buy(456, 456, 9999)
        self.assertEqual(self.api.calls[-1][0], 'sendMessage')


    def test_business_paid_media_sent_via_personal_chat(self):
        import time
        self.bot.handle_business_connection({
            'id': 'connection-A', 'user': {'id': 123}, 'is_enabled': True,
            'rights': {'can_reply': True}
        })
        self.bot.handle_business_message({
            'business_connection_id': 'connection-A',
            'chat': {'id': 456, 'type': 'private'},
            'from': {'id': 456, 'first_name': 'Test'},
            'date': int(time.time())
        })
        self.bot.send_business_paid_media(123, 456, self.item_id)
        method, sent = [x for x in self.api.calls if x[0]=='sendPaidMedia'][-1]
        self.assertEqual(sent['business_connection_id'], 'connection-A')
        self.assertEqual(sent['chat_id'], 456)
        self.assertEqual(sent['star_count'], 45)
        self.bot.purchased({'from': {'id': 456}, 'paid_media_payload': sent['payload']})
        self.assertTrue(self.db.purchased(456, self.item_id))
        # Business buyers are not sent unsolicited bot-DMs.
        self.assertEqual(self.api.calls[-1][1]['chat_id'], 123)

    def test_business_connection_is_admin_only_and_requires_recent_buyer(self):
        import time
        self.bot.handle_business_connection({
            'id':'other', 'user':{'id':999}, 'is_enabled':True,
            'rights':{'can_reply':True}
        })
        self.assertIsNone(self.db.business_connection('other'))
        self.bot.handle_business_connection({
            'id':'mine', 'user':{'id':123}, 'is_enabled':True,
            'rights':{'can_reply':True}
        })
        self.bot.handle_business_message({
            'business_connection_id':'mine', 'chat':{'id':456,'type':'private'},
            'from':{'id':456,'first_name':'Test'}, 'date':int(time.time())-90000
        })
        self.assertIsNone(self.db.eligible_business_buyer(456,123))
        self.bot.handle_message({'from': {'id':456},'chat':{'id':456,'type':'private'},
                                 'text':'/buyers'})
        self.assertNotIn('Select a buyer', self.api.calls[-1][1]['text'])

if __name__ == '__main__':
    unittest.main()
