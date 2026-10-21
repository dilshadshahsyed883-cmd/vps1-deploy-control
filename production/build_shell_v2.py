"""Content-address persistent adapter assets; retain old hashes for open tabs."""
from pathlib import Path
import hashlib
import json
ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / 'apps/scene-builder/part01/static'

def build(check=False):
    from production.shell_v2_contract import validate
    registry = json.loads((STATIC/'shell_v2_registry.json').read_text())
    validate(registry)
    outputs = {}
    def asset(name, text):
        digest=hashlib.sha256(text.encode()).hexdigest()[:20]
        stem,suffix=name.rsplit('.',1)
        path=f'/static/shell-v2/{stem}.{digest}.{suffix}'
        outputs[STATIC/path.removeprefix('/static/')]=text
        return path
    modules={}
    css={}
    for adapter,name in [('storage','storage_manager'),('dashboard','dashboard_control_center')]:
        modules[name+'.js']=asset(name+'.js',(STATIC/(name+'.js')).read_text())
        css[adapter]=asset(name+'.css',(STATIC/(name+'.css')).read_text())
    host=(STATIC/'shell_v2_host.js').read_text()
    for name,url in modules.items():
        host=host.replace("'./"+name+"'",json.dumps(url))
    host=host.replace('__SHELL_REGISTRY__',json.dumps(registry,separators=(',',':')))
    host=host.replace('__APP_STYLES__',json.dumps(css,separators=(',',':')))
    entry=asset('host.js',host)
    outputs[STATIC/'shell-v2/manifest.json']=json.dumps({'contract':2,'entry':entry,'assets':[entry,*modules.values(),*css.values()]},indent=2)+'\n'
    for path,text in outputs.items():
        if check:
            if not path.is_file() or path.read_text()!=text: raise ValueError(f'stale generated shell asset: {path.name}')
        else:
            path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text)
    return len(outputs)
if __name__=='__main__':
    import sys
    print('SHELL_ASSETS=PASS', build('--check' in sys.argv))
