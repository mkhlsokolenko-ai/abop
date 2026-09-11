import tarfile, os
def keep(p):
    b = os.path.basename(p)
    if b.startswith('_') or b.endswith('.bak') or b.endswith('.pyc') or '__pycache__' in p:
        return False
    return True
with tarfile.open('_abop_deploy.tar', 'w') as t:
    for d in ['server', 'cli', 'skills']:
        for root, dirs, files in os.walk(d):
            if '__pycache__' in root:
                continue
            for f in files:
                p = os.path.join(root, f)
                if keep(p):
                    t.add(p, arcname=p.replace(os.sep, '/'))
    t.add('webapp/index.html', arcname='webapp/index.html')
    t.add('Dockerfile.webapi', arcname='Dockerfile.webapi')
    t.add('_env.server', arcname='.env')
print('tar:', os.path.getsize('_abop_deploy.tar'), 'bytes')
