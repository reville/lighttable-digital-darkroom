"""Generate a separate, Sparkle-free Store project. Never changes direct project."""
from pathlib import Path
import re, plistlib
ROOT = Path(__file__).resolve().parents[2]
def configure(output):
    output.mkdir(parents=True, exist_ok=True)
    source = (ROOT / 'LightTable.xcodeproj/project.pbxproj').read_text()
    source = '\n'.join(line for line in source.splitlines() if 'Sparkle' not in line)
    source = re.sub(r'/\* Begin XCRemoteSwiftPackageReference section \*/.*?/\* End XCSwiftPackageProductDependency section \*/', '', source, flags=re.S)
    source = source.replace('INFOPLIST_FILE = app/Info.plist;', 'INFOPLIST_FILE = StoreInfo.plist;\n\t\t\t\tCODE_SIGN_ENTITLEMENTS = release/store/main.entitlements;\n\t\t\t\tENABLE_APP_SANDBOX = YES;\n\t\t\t\tSWIFT_ACTIVE_COMPILATION_CONDITIONS = "$(inherited) LIGHTTABLE_STORE";')
    (output / 'project.pbxproj').write_text(source)
    info = plistlib.loads((ROOT / 'app/Info.plist').read_bytes())
    info = {k:v for k,v in info.items() if not k.startswith('SU')}
    info['LightTableDistribution'] = 'mac-app-store'
    (ROOT / 'StoreInfo.plist').write_bytes(plistlib.dumps(info))
if __name__ == '__main__':
    configure(ROOT / 'LightTableStore.xcodeproj')
