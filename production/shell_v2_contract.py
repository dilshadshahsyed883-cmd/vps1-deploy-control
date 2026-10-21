"""Fail closed for registrations joining the persistent host.

Legacy gateways remain explicitly supported boundaries, not migrated adapters.
"""
from pathlib import Path
import re

def validate(registry):
    if registry.get('schema')!=2 or registry.get('shell_version')!=2 or registry.get('design_version')!=1:
        raise ValueError('incompatible shell/design contract')
    if registry.get('geometry')!={'sidebar':214,'mobile':760,'studio_max':1400,'rail':62}:
        raise ValueError('uncoordinated geometry change')
    ids=set(); paths=set()
    for app in registry['apps']:
        if app['id'] in ids or app['path'] in paths: raise ValueError('duplicate registration')
        ids.add(app['id']); paths.add(app['path'])
        if app['id']=='auto-script' or not re.fullmatch(r'/[a-zA-Z0-9/_-]*',app['path']): raise ValueError('unsafe route')
        if not app.get('owner'): raise ValueError('missing ownership')
        if app['mode']=='native-document':
            if not app.get('removal_condition') or app.get('auth')!='backend': raise ValueError('unbounded compatibility exception')
        elif app['mode']=='adapter':
            if app.get('contract')!=2 or app.get('design')!=1 or not app.get('smoke') or not app.get('legacy') or app.get('adapter') not in {'storage','dashboard'} or app.get('role') not in {'admin','authenticated'}:
                raise ValueError('incompatible adapter')
        else: raise ValueError('unsupported integration mode')
    if not {'dashboard','storage-manager'} <= ids: raise ValueError('required adapters missing')
    aliases=set(paths)
    for app in registry['apps']:
        for alias in app.get('aliases',[]):
            if not re.fullmatch(r'/[a-zA-Z0-9/_-]*',alias) or alias in aliases: raise ValueError('unsafe or duplicate alias')
            aliases.add(alias)

if __name__=='__main__':
    from production.build_shell_v2 import build
    build(check=True)
    print('SHELL_V2_CONTRACT=PASS production=NOT_VERIFIED')
