from unittest import mock

from django.test import SimpleTestCase

from tasaf_payment.events import emit_muse_event, muse_event


class MuseEventTest(SimpleTestCase):
    def test_listener_called_after_commit(self):
        received = []

        def listener(sender, **kwargs):
            received.append(kwargs)

        muse_event.connect(listener, dispatch_uid='test.muse_event')
        try:
            with mock.patch('tasaf_payment.events.transaction.on_commit', side_effect=lambda fn: fn()):
                emit_muse_event('P', 'batch_status', status='REJECTED', description='why')
        finally:
            muse_event.disconnect(dispatch_uid='test.muse_event')
        self.assertEqual(received[0]['event'], 'batch_status')
        self.assertEqual(received[0]['status'], 'REJECTED')
        self.assertEqual(received[0]['description'], 'why')

    def test_failing_listener_does_not_raise(self):
        def broken(sender, **kwargs):
            raise RuntimeError('boom')

        muse_event.connect(broken, dispatch_uid='test.muse_event.broken')
        try:
            with mock.patch('tasaf_payment.events.transaction.on_commit', side_effect=lambda fn: fn()):
                emit_muse_event('P', 'closed', status='CLOSED')
        finally:
            muse_event.disconnect(dispatch_uid='test.muse_event.broken')
