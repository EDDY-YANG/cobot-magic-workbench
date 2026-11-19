"""Real loopback HTTP plus synthetic data/backend; never model/robot evidence."""
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
import sys
import threading
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
from service import acceptance as a
from service.server import BF16Service,make_server
from test_acceptance import synthetic_source,FakeBackend,config,empty_receipt
from test_service import make_payload,NONCE,CONFIG_SHA


@contextmanager
def loopback_fixture():
    source=synthetic_source()
    jpeg=make_payload()['front_jpeg_base64']
    for sample in source.samples:
        sample.payload['front_jpeg_base64']=jpeg
        sample.payload['right_jpeg_base64']=jpeg
        sample.source_metadata.update(data_kind='SYNTHETIC_JPEG_AND_ZERO_ACTION_PROTOCOL_FIXTURE',
                                      request_payload_sha256=a.digest(sample.payload))
    api=BF16Service(FakeBackend(source),5000,service_nonce=NONCE,config_sha256=CONFIG_SHA)
    server=make_server(api,port=0)
    worker=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True)
    worker.start()
    transport=a.HTTPTransport(config())
    transport.port=server.server_address[1]  # Test-only ephemeral loopback listener.
    try:yield source,api,transport
    finally:
        api.begin_close();api.wait_idle(2);server.shutdown();server.server_close();worker.join(2)
        if worker.is_alive():raise AssertionError('Synthetic HTTP test worker did not exit')


class EndToEndTests(unittest.TestCase):
    def test_full_3_plus_100_over_actual_http_keeps_synthetic_scope_and_no_lease_secret(self):
        with loopback_fixture() as (source,api,transport):
            receipt=a.exercise(config(),source,transport,empty_receipt(),heartbeat=True,
                               owner_binding={'nonce':NONCE,'config_sha256':CONFIG_SHA})
            self.assertEqual(receipt['status'],'SYNTHETIC_PROTOCOL_TEST_ONLY')
            self.assertFalse(receipt['historical_service_validated'])
            self.assertEqual(len(receipt['records']),103)
            self.assertEqual(receipt['completed_measured'],100)
            self.assertTrue(receipt['golden_replay_bitwise_verified'])
            self.assertFalse(api.status()['lease']['active'])
            self.assertNotIn('lease_token',json.dumps(receipt))
            self.assertTrue(any(row['rtc']['enabled'] for row in receipt['records']))
            self.assertTrue(any(not row['rtc']['enabled'] for row in receipt['records']))

    def test_wrong_owner_rejected_before_acquire_over_actual_http(self):
        with loopback_fixture() as (source,api,transport):
            receipt=empty_receipt()
            with self.assertRaises(a.AcceptanceError):
                a.exercise(config(),source,transport,receipt,
                           owner_binding={'nonce':'f'*32,'config_sha256':CONFIG_SHA})
            self.assertEqual(receipt['records'],[])
            self.assertFalse(api.status()['lease']['active'])

    def test_golden_bit_mismatch_retains_first_failed_attempt_and_releases_lease(self):
        with loopback_fixture() as (source,api,transport):
            # Deliberately corrupt only the client's comparison copy. The
            # synthetic backend still returns its separate +0 float32 array.
            comparison=deepcopy(source)
            comparison.samples[0].golden_actions[0,0]=-0.0
            receipt=empty_receipt()
            with self.assertRaisesRegex(a.AcceptanceError,'bitwise mismatch'):
                a.exercise(config(),comparison,transport,receipt,
                           owner_binding={'nonce':NONCE,'config_sha256':CONFIG_SHA})
            self.assertEqual(len(receipt['records']),1)
            self.assertEqual(receipt['records'][0]['status'],'FAILED_REQUEST_RETAINED')
            self.assertFalse(receipt['records'][0]['golden_F32_bytes_equal_including_signed_zero'])
            self.assertFalse(api.status()['lease']['active'])


if __name__=='__main__':unittest.main()
