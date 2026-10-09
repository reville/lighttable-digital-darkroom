# SPDX-License-Identifier: GPL-3.0-only
"""Read-only sandbox/signature audit of a fully assembled Store candidate."""
from pathlib import Path
import json, plistlib, subprocess, sys
app=Path(sys.argv[1]).resolve()
info=plistlib.loads((app/'Contents/Info.plist').read_bytes())
assert info.get('LightTableDistribution')=='mac-app-store'
assert not any(key.startswith('SU') for key in info)
assert not list(app.rglob('Sparkle.framework'))
subprocess.run(['codesign','--verify','--deep','--strict',str(app)],check=True)
magic={b'\xcf\xfa\xed\xfe',b'\xce\xfa\xed\xfe',b'\xfe\xed\xfa\xcf',b'\xfe\xed\xfa\xce',b'\xca\xfe\xba\xbe',b'\xbe\xba\xfe\xca',b'\xca\xfe\xba\xbf',b'\xbf\xba\xfe\xca'}
executables=[]
exceptions=[]
for f in sorted((app/'Contents').rglob('*')):
 if f.is_symlink() or not f.is_file():continue
 with f.open('rb') as stream: head=stream.read(4)
 if head not in magic:continue
 kind=subprocess.check_output(['file','-b',str(f)],text=True,errors='replace')
 result=subprocess.run(['codesign','-d','--entitlements',':-',str(f)],capture_output=True,check=True)
 ent=plistlib.loads(result.stdout) if result.stdout.strip() else {}
 if '--require-hardened' in sys.argv:
  signature=subprocess.run(['codesign','-d','--verbose=4',str(f)],capture_output=True,text=True,check=True)
  assert 'runtime)' in signature.stderr,str(f)
 if 'executable' not in kind:
  assert not ent,(str(f),ent)
  continue
 assert ent.get('com.apple.security.app-sandbox') is True,str(f)
 if f!=app/'Contents/MacOS/LightTable':
  expected={'com.apple.security.app-sandbox':True,'com.apple.security.inherit':True}
  if f==app/'Contents/Resources/Python/bin/python3.13':
   expected['com.apple.security.cs.allow-unsigned-executable-memory']=True
   exceptions.append(str(f.relative_to(app)))
  assert ent==expected,(str(f),ent)
 else:
  assert ent.get('com.apple.security.personal-information.photos-library') is True,str(f)
  assert not any('temporary-exception' in k or 'absolute-path' in k for k in ent)
  assert not any(k.startswith('com.apple.security.cs.') for k in ent),(str(f),ent)
 executables.append(str(f.relative_to(app)))
assert executables
assert len(exceptions)==1,exceptions
print(json.dumps({'source_revision':info.get('LightTableSourceRevision'),'source_dirty':info.get('LightTableSourceDirty'),'version':info.get('CFBundleShortVersionString'),'deep_strict_signature':True,'sparkle_absent':True,'hardened_runtime_required':'--require-hardened' in sys.argv,'photos_library_entitlement_main_only':True,'unsigned_executable_memory_only':exceptions,'sandboxed_executables':executables},indent=2))
