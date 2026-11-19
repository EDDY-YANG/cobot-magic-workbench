"""PC package/dry-run tests, temporary source directories only, no real SSH."""
from contextlib import ExitStack
import hashlib
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.request

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'pc'))
import bf16_control as pc


class PCControlTests(unittest.TestCase):
    def setUp(self):
        self.scope=ExitStack();self.addCleanup(self.scope.close)
        def forbidden(*args,**kwargs):raise AssertionError('PC control tests forbid actual SSH/process/network')
        for owner,name in ((socket,'create_connection'),(socket.socket,'connect'),(subprocess,'Popen'),(subprocess,'run'),(urllib.request,'urlopen')):
            self.scope.enter_context(patch.object(owner,name,forbidden))
    def fixture(self,folder):
        root=Path(folder)/'source';root.mkdir()
        files={'service_config.json':'{}','service/server.py':'# SYNTHETIC SOURCE ONLY\n','service/control.py':'# SYNTHETIC SOURCE ONLY\n'}
        for name,text in files.items():(root/name).parent.mkdir(parents=True,exist_ok=True);(root/name).write_text(text,encoding='utf-8')
        value={'schema':pc.MANIFEST_SCHEMA,'files':{name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in files}}
        path=root/'source_manifest.json';path.write_text(json.dumps(value),encoding='utf-8');return root,path
    def args(self,action='inspect',online=False):
        return SimpleNamespace(config=ROOT/'service_config.json',manifest=ROOT/'source_manifest.json',action=action,
            execute_online=online,ssh_target='lab-4090',ssh_config=Path('SYNTHETIC_CONFIG_NOT_READ_IN_DRY_RUN'),host_name=None,
            step=10000,run_dir=None,handover=None)
    def test_default_all_actions_never_construct_network_or_subprocess(self):
        def fail(*args,**kwargs):raise AssertionError('dry run called transport')
        for action in ('inspect','upload','prepare-handover','start','status','stop','rollback','accept'):
            result=pc.dispatch(self.args(action),run=fail)
            self.assertEqual(result['status'],'DRY_RUN_NO_REMOTE_NO_MODEL_NO_GPU')
            self.assertTrue(result['remote_root'].startswith('/data/'))
    def test_source_payload_is_deterministic_and_rejects_overwrite(self):
        with tempfile.TemporaryDirectory(prefix='synthetic-source-package-') as folder:
            root,path=self.fixture(folder);payload,meta=pc.build_source_payload(root,path)
            second,meta2=pc.build_source_payload(root,path);self.assertEqual(payload,second);self.assertEqual(meta,meta2)
            target=Path(folder)/'new-independent-root'
            result=pc.unpack_source_payload(payload,target,meta['manifest_sha256'])
            self.assertEqual(result['status'],'SOURCE_ONLY_UPLOAD_SHA_VERIFIED')
            self.assertEqual((target/'source_manifest.json').read_bytes(),path.read_bytes())
            before=(target/'service/server.py').read_bytes()
            with self.assertRaises(FileExistsError):pc.unpack_source_payload(payload,target,meta['manifest_sha256'])
            self.assertEqual((target/'service/server.py').read_bytes(),before)
    def test_source_sha_change_fails_before_package_or_new_root(self):
        with tempfile.TemporaryDirectory(prefix='synthetic-source-sha-') as folder:
            root,path=self.fixture(folder);(root/'service/server.py').write_text('changed')
            with self.assertRaisesRegex(ValueError,'source SHA differs'):pc.build_source_payload(root,path)
    def test_manifest_forbids_weights_runtime_escape_and_self_hash(self):
        for name in ('../escape.py','/absolute.py','weights/model.json','runs/secret.py','source_manifest.json','service/model.safetensors'):
            value={'schema':pc.MANIFEST_SCHEMA,'files':{name:'a'*64,'service_config.json':'b'*64,'service/server.py':'c'*64,'service/control.py':'d'*64}}
            with self.subTest(name=name),self.assertRaises(ValueError):pc.checked_manifest(value)
    def test_archive_symlink_and_traversal_rejected_before_root_creation(self):
        with tempfile.TemporaryDirectory(prefix='synthetic-unsafe-archive-') as folder:
            for name,kind in (('../escape.py',tarfile.REGTYPE),('source_manifest.json',tarfile.SYMTYPE)):
                buffer=io.BytesIO()
                with tarfile.open(fileobj=buffer,mode='w:gz') as archive:
                    info=tarfile.TarInfo(name);info.type=kind;info.linkname='/etc/passwd';info.size=0
                    archive.addfile(info,io.BytesIO(b''))
                    if name!='source_manifest.json':
                        info=tarfile.TarInfo('source_manifest.json');info.size=2;archive.addfile(info,io.BytesIO(b'{}'))
                target=Path(folder)/'must-not-be-created'
                with self.assertRaises(ValueError):pc.unpack_source_payload(buffer.getvalue(),target,'a'*64)
                self.assertFalse(target.exists())
    def test_wrong_manifest_digest_keeps_target_absent(self):
        with tempfile.TemporaryDirectory(prefix='synthetic-digest-') as folder:
            root,path=self.fixture(folder);payload,_=pc.build_source_payload(root,path);target=Path(folder)/'not-created'
            with self.assertRaisesRegex(ValueError,'manifest transport digest'):pc.unpack_source_payload(payload,target,'0'*64)
            self.assertFalse(target.exists())
    def test_existing_pinned_config_and_hostname_are_argv_only(self):
        with tempfile.TemporaryDirectory(prefix='synthetic-sshconfig-') as folder:
            path=Path(folder)/'config';path.write_text('Host lab-4090\n HostKeyAlias SYNTHETIC_PIN\n')
            before=path.read_bytes();argv=pc.ssh_prefix('lab-4090',path,'192.0.2.10')
            self.assertIn('-F',argv);self.assertIn(str(path),argv);self.assertIn('HostName=192.0.2.10',argv)
            self.assertIn('StrictHostKeyChecking=yes',argv);self.assertEqual(path.read_bytes(),before)
            self.assertIn('ConnectTimeout=8',argv);self.assertIn('ServerAliveInterval=10',argv);self.assertIn('ServerAliveCountMax=3',argv)
    def test_alias_or_hostname_command_injection_is_rejected(self):
        for target in ('-oProxyCommand=bad','lab-4090;bad','lab-4090 bad'):
            with self.assertRaises(ValueError):pc.ssh_prefix(target)
        with self.assertRaises(ValueError):pc.ssh_prefix('lab-4090',host_name='host;bad')
    def test_upload_unknown_outcome_does_not_retry(self):
        args=self.args('upload',True)
        with tempfile.TemporaryDirectory(prefix='synthetic-upload-outcome-') as folder:
            root,path=self.fixture(folder);config=Path(folder)/'sshconfig';config.write_text('Host lab-4090\n')
            args.manifest=path;args.ssh_config=config;calls=[]
            def timeout(*a,**k):calls.append(True);raise subprocess.TimeoutExpired('SYNTHETIC_SSH_NOT_EXECUTED',180)
            with patch.object(pc,'ROOT',root):result=pc.dispatch(args,run=timeout)
            self.assertEqual(result['status'],'UPLOAD_OUTCOME_UNKNOWN_CHECK_REMOTE_DO_NOT_RETRY_OVERWRITE');self.assertEqual(len(calls),1)
    def test_remote_script_compiles_without_execution(self):
        script=pc.remote_upload_script();compile(script,'synthetic_remote_upload_source','exec')
        self.assertNotIn('extractall(',script);self.assertIn('root.mkdir(mode=0o700)',script)


if __name__=='__main__':unittest.main()
