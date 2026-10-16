"""Deterministic offline storage-manager pilot and fail-closed integration tests."""
import hashlib,io,json,tarfile,tempfile,threading,unittest
from pathlib import Path
from rebuild.shadow_release import ShadowBlocked,admit,rollback_shadow,shadow_install
A="a"*40
B="b"*40

def artifact(folder,app,sha):
    body=b"safe application source\n"
    tarname=app+"-"+sha[:12]+".tar.gz"
    p=folder/tarname
    with tarfile.open(p,"w:gz") as t:
        entry=tarfile.TarInfo(app+"/app.txt")
        entry.size=len(body)
        t.addfile(entry,io.BytesIO(body))
    mp=folder/(tarname+".manifest.json")
    mp.write_text(json.dumps({"app":app,"commit_sha":sha,"archive":tarname,
        "sha256":hashlib.sha256(p.read_bytes()).hexdigest(),
        "files":[{"path":"app.txt","sha256":hashlib.sha256(body).hexdigest()}]}))
    return mp

def admission(app,sha,deps=None):
    return dict(app_id=app,source_sha=sha,shadow_port=8149,loopback_only=True,
       backup_restore_proven=True,capacity_current=True,runtime_verified=True,
       auth_negative_tested=True,worker_ownership_verified=True,
       rollback_proven=True,port_ownership_verified=True,security_reviewed=True,
       free_bytes=20_000_000,peak_new_bytes=1_000_000,
       host_reserve_bytes=5_000_000,rollback_bytes=5_000_000,
       verified_dependencies=deps or [])

class OfflineShadowIntegration(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory(prefix="vps1-tab3-",dir="/tmp")
        self.addCleanup(t.cleanup)
        self.root=Path(t.name)/"shadow"
        self.fixtures=Path(t.name)
        self.app="vps-storage-manager"
        self.manifest=artifact(self.fixtures,self.app,B)
        self.healthy=lambda app,path,phase:True
    def install(self,e=None,probe=None,ports=()):
        return shadow_install(self.root,self.app,B,self.manifest,e or admission(self.app,B),
                              probe or self.healthy,ports)
    def test_immutable_verified_and_idempotent(self):
        self.assertEqual(self.install()["status"],"SHADOW_VERIFIED")
        self.assertEqual(self.install()["status"],"NO_CHANGE_HEALTHY")
        self.assertEqual((self.root/"shadow-current"/self.app).resolve().name,B)
    def test_integrity_failure(self):
        (self.fixtures/(self.app+"-"+B[:12]+".tar.gz")).write_bytes(b"BAD")
        with self.assertRaises(ShadowBlocked):self.install()
    def test_fail_closed_backup_auth_workers_and_security(self):
        for gate in ("backup_restore_proven","runtime_verified","security_reviewed",
                     "auth_negative_tested","worker_ownership_verified","rollback_proven"):
            e=admission(self.app,B);e[gate]=False
            with self.subTest(gate=gate),self.assertRaises(ShadowBlocked):self.install(e=e)
    def test_insufficient_capacity(self):
        e=admission(self.app,B);e["peak_new_bytes"]=18_000_000
        with self.assertRaisesRegex(ShadowBlocked,"capacity"):self.install(e=e)
    def test_port_conflict_and_public_bind(self):
        with self.assertRaisesRegex(ShadowBlocked,"port"):self.install(ports=[8149])
        e=admission(self.app,B);e["loopback_only"]=False
        with self.assertRaises(ShadowBlocked):self.install(e=e)
    def test_dependency_order(self):
        m=artifact(self.fixtures,"scene-builder",B)
        e=admission("scene-builder",B)
        with self.assertRaisesRegex(ShadowBlocked,"dependency"):
            admit("scene-builder",B,m,e)
        e["verified_dependencies"]=["vps-storage-manager"]
        self.assertEqual(admit("scene-builder",B,m,e)["app"],"scene-builder")
    def test_failing_stage_aborts_without_pointer(self):
        with self.assertRaisesRegex(ShadowBlocked,"ABORTED"):
            self.install(probe=lambda a,p,s:False)
        self.assertFalse((self.root/"shadow-current"/self.app).exists())
    def test_post_flip_rollback_restores_old(self):
        m=artifact(self.fixtures,self.app,A)
        shadow_install(self.root,self.app,A,m,admission(self.app,A),self.healthy)
        with self.assertRaisesRegex(ShadowBlocked,"ROLLED_BACK"):
            self.install(probe=lambda a,p,s:s!="shadow")
        self.assertEqual((self.root/"shadow-current"/self.app).resolve().name,A)
    def test_first_shadow_failure_clears_pointer(self):
        with self.assertRaisesRegex(ShadowBlocked,"ROLLED_BACK"):
            self.install(probe=lambda a,p,s:s=="staged")
        self.assertFalse((self.root/"shadow-current"/self.app).exists())
    def test_explicit_rollback(self):
        m=artifact(self.fixtures,self.app,A)
        shadow_install(self.root,self.app,A,m,admission(self.app,A),self.healthy)
        self.install()
        self.assertEqual(rollback_shadow(self.root,self.app,A,self.healthy)["status"],"ROLLED_BACK")
    def test_concurrent_duplicate_serialized(self):
        states=[]
        def go():states.append(self.install()["status"])
        x=threading.Thread(target=go);y=threading.Thread(target=go)
        x.start();y.start();x.join();y.join()
        self.assertEqual(sorted(states),["NO_CHANGE_HEALTHY","SHADOW_VERIFIED"])
    def test_production_path_and_offline_app_denied(self):
        with self.assertRaises(ShadowBlocked):
            shadow_install("/opt/vps",self.app,B,self.manifest,admission(self.app,B),self.healthy)
        m=artifact(self.fixtures,"auto-script",B)
        with self.assertRaises(ShadowBlocked):admit("auto-script",B,m,admission("auto-script",B))

if __name__=="__main__":unittest.main()
