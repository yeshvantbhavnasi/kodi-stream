#!/usr/bin/env python3
"""Rebuild the Kodi repository files under zips/ from the addon folders. Run after changing an addon's version."""
import hashlib
import os
import re
import shutil
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
ADDONS = ['script.stream', 'repository.stream']
SKIP = ('__pycache__', '.DS_Store')


def build():
    out = os.path.join(ROOT, 'zips')
    shutil.rmtree(out, ignore_errors=True)
    entries = []
    for addon in ADDONS:
        xml = open(os.path.join(ROOT, addon, 'addon.xml'), encoding='utf-8').read()
        version = re.search(r'<addon[^>]*\bversion="([^"]+)"', xml).group(1)
        entries.append(re.sub(r'<\?xml[^>]*\?>\s*', '', xml).strip())
        os.makedirs(os.path.join(out, addon))
        target = os.path.join(out, addon, '{0}-{1}.zip'.format(addon, version))
        with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as z:
            for folder, _, files in os.walk(os.path.join(ROOT, addon)):
                for name in files:
                    path = os.path.join(folder, name)
                    if not any(s in path for s in SKIP):
                        z.write(path, os.path.relpath(path, ROOT))
        # Kodi shows these in the repository listing before the addon is installed.
        for art in ('icon.png', 'fanart.jpg'):
            if os.path.exists(os.path.join(ROOT, addon, art)):
                shutil.copy(os.path.join(ROOT, addon, art), os.path.join(out, addon, art))
        shots = os.path.join(ROOT, addon, 'resources', 'screenshots')
        if os.path.isdir(shots):
            shutil.copytree(shots, os.path.join(out, addon, 'resources', 'screenshots'))
        if addon.startswith('repository.'):
            # Copy at the top level too: this is the file people install first.
            for old in os.listdir(ROOT):
                if old.startswith(addon + '-') and old.endswith('.zip'):
                    os.remove(os.path.join(ROOT, old))
            shutil.copy(target, ROOT)
            link = os.path.basename(target)
    index = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<addons>\n' + '\n'.join(entries) + '\n</addons>\n'
    open(os.path.join(out, 'addons.xml'), 'w', encoding='utf-8').write(index)
    open(os.path.join(out, 'addons.xml.md5'), 'w').write(hashlib.md5(index.encode('utf-8')).hexdigest())
    # Kodi's file manager reads plain links from this page when the folder is added as a source.
    open(os.path.join(ROOT, 'index.html'), 'w').write(
        '<!DOCTYPE html>\n<html><body>\n<h1>Stream repository for Kodi</h1>\n<a href="{0}">{0}</a>\n</body></html>\n'.format(link))


if __name__ == '__main__':
    build()
