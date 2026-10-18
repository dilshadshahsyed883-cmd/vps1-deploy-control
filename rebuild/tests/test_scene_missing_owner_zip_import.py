"""Source-only recover importer: verified manifest, no overwrite, idempotence."""
import hashlib
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
import zipfile

ROOT=Path(__file__).resolve().parents[2]
LOADER=ROOT/"rebuild/tools/recover_scene_missing_from_owner_zip.py"
_spec=importlib.util.spec_from_file_location("scene_missing_importer",LOADER)
mod=importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

class ImportMissingSceneModules(unittest.TestCase):
    def test_dry_run_then_recovery_then_idempotent_second_run(self):
        with tempfile.TemporaryDirectory(prefix="vps1-scene-src-") as td:
            p=Path(td)
            target=p/"apps/scene-builder/part02"
            target.mkdir(parents=True)
            (target/"api_server.py").write_text("# original app\n")
            smart=target/"smart_scroll"
            smart.mkdir()
            original=smart/"v2_renderer.py"
            original.write_text("VALUE = 'existing version'\n")
            archive=p/"owner-archive.zip"
            modules={"app/smart_scroll/"+name+".py":("VALUE = "+repr(name)+"\n").encode()
                     for name in mod.MODULES}
            fillers={"safepath/"+str(i)+".txt": b"nonsecret test" for i in range(84-len(modules))}
            content=dict(modules,**fillers)
            manifest="\n".join(hashlib.sha256(raw).hexdigest()+"  ./"+k for k,raw in content.items())+"\n"
            with zipfile.ZipFile(archive,"w",compression=zipfile.ZIP_DEFLATED) as zipfile_out:
                for key,raw in content.items():
                    zipfile_out.writestr(key,raw)
                zipfile_out.writestr("FILE_SHA256SUMS.txt",manifest)
            dry=mod.recover(archive,p)
            self.assertEqual(dry["manifest_files_verified"],84)
            self.assertEqual(len(dry["missing"]),len(mod.MODULES)-1)
            self.assertEqual(dry["added"],[])
            self.assertEqual(original.read_text(),"VALUE = 'existing version'\n")
            actual=mod.recover(archive,p,apply=True)
            self.assertEqual(len(actual["added"]),len(mod.MODULES)-1)
            self.assertEqual(len(actual["existing_preserved"]),1)
            self.assertFalse(actual["existing_preserved"][0]["same_as_zip"])
            self.assertEqual(original.read_text(),"VALUE = 'existing version'\n")
            repeat=mod.recover(archive,p,apply=True)
            self.assertEqual(repeat["added"],[])
            self.assertEqual(len(repeat["existing_preserved"]),len(mod.MODULES))
            # Tampering with verified archive must fail before any file write.
            tampered=p/"bad.zip"
            with zipfile.ZipFile(archive) as original_zip, zipfile.ZipFile(tampered,"w") as rewritten:
                for item in original_zip.infolist():
                    data=original_zip.read(item.filename)
                    if item.filename=="app/smart_scroll/transition_engine.py":
                        data=b"VALUE = 'tampered'\n"
                    rewritten.writestr(item.filename,data)
            with self.assertRaisesRegex(RuntimeError,"SHA mismatch"):
                mod.recover(tampered,p,apply=True)

if __name__=="__main__":
    unittest.main()
